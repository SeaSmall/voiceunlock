#include "VoiceCredentialProvider.h"
#include "VoiceCredential.h"
#include "helpers.h"
#include "PipeClient.h"
#include <new>
#include <objbase.h>

namespace VoiceUnlock {

VoiceCredentialProvider::VoiceCredentialProvider()
    : m_cRef(1), m_cpus(CPUS_INVALID), m_pCred(nullptr) {}

VoiceCredentialProvider::~VoiceCredentialProvider() {
    StopWatcher();
    if (m_gitCookie) {
        IGlobalInterfaceTable* git = nullptr;
        if (SUCCEEDED(CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr,
                                       CLSCTX_INPROC_SERVER, IID_IGlobalInterfaceTable,
                                       (void**)&git)) && git) {
            git->RevokeInterfaceFromGlobal(m_gitCookie);
            git->Release();
        }
        m_gitCookie = 0;
    }
    if (m_pEvents) { m_pEvents->Release(); m_pEvents = nullptr; }
    if (m_pCred) { m_pCred->Release(); m_pCred = nullptr; }
}

IFACEMETHODIMP VoiceCredentialProvider::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProvider) {
        *ppv = static_cast<ICredentialProvider*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}
IFACEMETHODIMP_(ULONG) VoiceCredentialProvider::AddRef()  { return InterlockedIncrement(&m_cRef); }
IFACEMETHODIMP_(ULONG) VoiceCredentialProvider::Release() { LONG c = InterlockedDecrement(&m_cRef); if (c == 0) delete this; return c; }

IFACEMETHODIMP VoiceCredentialProvider::SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus, DWORD) {
    // We only participate in logon and unlock.
    switch (cpus) {
        case CPUS_LOGON:
        case CPUS_UNLOCK_WORKSTATION:
            m_cpus = cpus;
            if (!m_pCred) {
                m_pCred = new (std::nothrow) VoiceCredential();
                if (!m_pCred) return E_OUTOFMEMORY;
                HRESULT hr = m_pCred->Initialize(cpus);
                if (FAILED(hr)) { m_pCred->Release(); m_pCred = nullptr; return hr; }
            }
            EnsureWatcher();
            return S_OK;
        default:
            return E_NOTIMPL;
    }
}

IFACEMETHODIMP VoiceCredentialProvider::SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) {
    return E_NOTIMPL;
}

IFACEMETHODIMP VoiceCredentialProvider::Advise(ICredentialProviderEvents* pEvents, UINT_PTR upAdviseContext) {
    // ★ 这个接口是"零界面占用"的关键：票据备好之前我们一个凭据都不枚举，
    //   界面上完全没有我们；备好之后靠 CredentialsChanged 让 LogonUI 重新枚举，
    //   那一个凭据才出现并被 pbAutoLogonWithDefault 自动提交。
    //   要跨线程用它，必须经 GIT 封送（CP 是 Apartment 模型）。
    if (!pEvents) return E_INVALIDARG;
    if (m_pEvents) { m_pEvents->Release(); m_pEvents = nullptr; }
    m_pEvents = pEvents;
    m_pEvents->AddRef();
    m_adviseContext = upAdviseContext;

    if (m_gitCookie) {
        IGlobalInterfaceTable* git = nullptr;
        if (SUCCEEDED(CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr,
                                       CLSCTX_INPROC_SERVER, IID_IGlobalInterfaceTable,
                                       (void**)&git)) && git) {
            git->RevokeInterfaceFromGlobal(m_gitCookie);
            git->Release();
        }
        m_gitCookie = 0;
    }
    IGlobalInterfaceTable* git = nullptr;
    if (SUCCEEDED(CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr,
                                   CLSCTX_INPROC_SERVER, IID_IGlobalInterfaceTable,
                                   (void**)&git)) && git) {
        git->RegisterInterfaceInGlobal(m_pEvents, IID_ICredentialProviderEvents, &m_gitCookie);
        git->Release();
    }
    EnsureWatcher();
    return S_OK;
}

IFACEMETHODIMP VoiceCredentialProvider::UnAdvise() {
    if (m_pEvents) { m_pEvents->Release(); m_pEvents = nullptr; }
    m_adviseContext = 0;
    if (m_gitCookie) {
        IGlobalInterfaceTable* git = nullptr;
        if (SUCCEEDED(CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr,
                                       CLSCTX_INPROC_SERVER, IID_IGlobalInterfaceTable,
                                       (void**)&git)) && git) {
            git->RevokeInterfaceFromGlobal(m_gitCookie);
            git->Release();
        }
        m_gitCookie = 0;
    }
    StopWatcher();
    return S_OK;
}

IFACEMETHODIMP VoiceCredentialProvider::GetFieldDescriptorCount(DWORD* pdwCount) {
    *pdwCount = FIELD_COUNT;
    return S_OK;
}

IFACEMETHODIMP VoiceCredentialProvider::GetFieldDescriptorAt(DWORD dwIndex,
                                                            CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) {
    if (dwIndex >= FIELD_COUNT) return E_INVALIDARG;
    CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR src = s_FieldDescriptors[dwIndex];
    auto* out = (CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR*)CoTaskMemAlloc(sizeof(src));
    if (!out) return E_OUTOFMEMORY;
    *out = src;
    // Label string must be its own CoTaskMem allocation.
    size_t cch = wcslen(src.pszLabel) + 1;
    out->pszLabel = (PWSTR)CoTaskMemAlloc(cch * sizeof(WCHAR));
    if (!out->pszLabel) { CoTaskMemFree(out); return E_OUTOFMEMORY; }
    wcscpy_s(out->pszLabel, cch, src.pszLabel);
    *ppcpfd = out;
    return S_OK;
}

IFACEMETHODIMP VoiceCredentialProvider::GetCredentialCount(DWORD* pdwCount, DWORD* pdwDefault,
                                                          BOOL* pbAutoLogonWithDefault) {
    // ★ 平时枚举 0 个凭据 —— 界面上【完全没有我们】。
    //   这是"原锁屏界面一点都不要动"的实现方式：ShittimLogon 的主题界面
    //   一个像素都不受影响；我们连磁贴都不出现。
    //   只有 agent 把声纹票据备好时，看门线程才置 m_ticketReady 并调
    //   CredentialsChanged，于是这里变成 1，并因 autoLogon=TRUE 被立刻自动提交。
    if (!m_pCred || !m_ticketReady.load()) {
        *pdwCount = 0;
        *pdwDefault = CREDENTIAL_PROVIDER_NO_DEFAULT;
        *pbAutoLogonWithDefault = FALSE;
        return S_OK;
    }
    *pdwCount = 1;
    // 有票据才自动提交。autoLogon 必须与【有界】的 GetSerialization 配对：
    // 早期版本在这里阻塞 25~60 秒，把登录界面占得像是没有密码框（真实踩过两次）。
    *pdwDefault = 0;
    *pbAutoLogonWithDefault = TRUE;
    return S_OK;
}

IFACEMETHODIMP VoiceCredentialProvider::GetCredentialAt(DWORD dwIndex, ICredentialProviderCredential** ppcpc) {
    if (dwIndex != 0 || !m_pCred || !m_ticketReady.load()) return E_INVALIDARG;
    return m_pCred->QueryInterface(IID_ICredentialProviderCredential, (void**)ppcpc);
}

// ---------------------------------------------------------------- 看门线程

void VoiceCredentialProvider::EnsureWatcher() {
    if (m_watchStarted) return;
    m_watchStarted = true;
    m_stopThread.store(false);
    m_watch = std::thread(&VoiceCredentialProvider::WatchLoop, this);
}

void VoiceCredentialProvider::StopWatcher() {
    if (!m_watchStarted) return;
    m_stopThread.store(true);
    if (m_watch.joinable()) m_watch.join();
    m_watchStarted = false;
}

void VoiceCredentialProvider::WatchLoop() {
    bool notified = false;
    while (!m_stopThread.load()) {
        bool ready = false;
        if (PingTicketReady(ready)) {
            m_ticketReady.store(ready);
            // 只在"状态真的变了"且 GIT 已经就绪时通知 LogonUI。
            // 注意 notified 是线程内的状态：即使票据在 Advise 之前就备好了，
            // 这里也会在拿到 cookie 之后补发一次通知。
            if (ready != notified && m_gitCookie) {
                IGlobalInterfaceTable* git = nullptr;
                if (SUCCEEDED(CoCreateInstance(CLSID_StdGlobalInterfaceTable, nullptr,
                                               CLSCTX_INPROC_SERVER,
                                               IID_IGlobalInterfaceTable, (void**)&git)) && git) {
                    ICredentialProviderEvents* ev = nullptr;
                    if (SUCCEEDED(git->GetInterfaceFromGlobal(
                            m_gitCookie, IID_ICredentialProviderEvents, (void**)&ev)) && ev) {
                        ev->CredentialsChanged(m_adviseContext);
                        ev->Release();
                        notified = ready;
                    }
                    git->Release();
                }
            }
        }
        // 票据就绪时提交通常立刻发生，可以慢点轮询；没票据时勤快点，免得反应迟钝。
        int steps = m_ticketReady.load() ? 8 : 3;
        for (int i = 0; i < steps && !m_stopThread.load(); ++i) Sleep(100);
    }
}

}  // namespace VoiceUnlock


