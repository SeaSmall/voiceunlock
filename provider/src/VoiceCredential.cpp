#include "VoiceCredential.h"
#include "helpers.h"
#include "PipeClient.h"
#include "guid.h"
#include <shlwapi.h>

namespace VoiceUnlock {

static HRESULT AllocStr(PCWSTR src, PWSTR* dst) {
    size_t cch = wcslen(src) + 1;
    *dst = (PWSTR)CoTaskMemAlloc(cch * sizeof(WCHAR));
    if (!*dst) return E_OUTOFMEMORY;
    wcscpy_s(*dst, cch, src);
    return S_OK;
}

VoiceCredential::VoiceCredential()
    : m_cRef(1), m_cpus(CPUS_INVALID), m_pEvents(nullptr),
      m_label(L"Voice Unlock"), m_status(L"Press the arrow, then speak") {}

VoiceCredential::~VoiceCredential() {}

HRESULT VoiceCredential::Initialize(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus) {
    m_cpus = cpus;
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::QueryInterface(REFIID riid, void** ppv) {
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (riid == IID_IUnknown || riid == IID_ICredentialProviderCredential) {
        *ppv = static_cast<ICredentialProviderCredential*>(this);
        AddRef();
        return S_OK;
    }
    return E_NOINTERFACE;
}
IFACEMETHODIMP_(ULONG) VoiceCredential::AddRef()  { return InterlockedIncrement(&m_cRef); }
IFACEMETHODIMP_(ULONG) VoiceCredential::Release() { LONG c = InterlockedDecrement(&m_cRef); if (c == 0) delete this; return c; }

IFACEMETHODIMP VoiceCredential::Advise(ICredentialProviderCredentialEvents* e) {
    if (m_pEvents) m_pEvents->Release();
    m_pEvents = e;
    if (e) e->AddRef();
    return S_OK;
}
IFACEMETHODIMP VoiceCredential::UnAdvise() {
    if (m_pEvents) { m_pEvents->Release(); m_pEvents = nullptr; }
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::SetSelected(BOOL* pbAutoLogon) {
    // Do NOT auto-trigger verification when the tile becomes selected.
    // The user must press the submit arrow to start a scan — this prevents
    // continuous re-scanning whenever the lock screen redraws or the tile
    // is re-selected, and gives the user explicit control over each attempt.
    *pbAutoLogon = TRUE;
    m_status = L"Press the arrow, then speak for about 6 seconds";
    if (m_pEvents) m_pEvents->SetFieldString(this, FIELD_STATUS, m_status.c_str());
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::SetDeselected() { return S_OK; }

IFACEMETHODIMP VoiceCredential::GetFieldState(DWORD dwFieldID,
                                             CREDENTIAL_PROVIDER_FIELD_STATE* pcpfs,
                                             CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE* pcpfis) {
    if (dwFieldID >= FIELD_COUNT) return E_INVALIDARG;
    *pcpfs  = s_FieldStatePairs[dwFieldID].cpfs;
    *pcpfis = s_FieldStatePairs[dwFieldID].cpfis;
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::GetStringValue(DWORD dwFieldID, PWSTR* ppwsz) {
    switch (dwFieldID) {
        case FIELD_LABEL:  return AllocStr(m_label.c_str(),  ppwsz);
        case FIELD_STATUS: return AllocStr(m_status.c_str(), ppwsz);
    }
    return E_INVALIDARG;
}

IFACEMETHODIMP VoiceCredential::GetBitmapValue(DWORD dwFieldID, HBITMAP* phbmp) {
    // Use default logo — custom bitmap omitted in MVP.
    if (dwFieldID != FIELD_TILE_IMAGE) return E_INVALIDARG;
    *phbmp = nullptr;
    return E_NOTIMPL;
}

IFACEMETHODIMP VoiceCredential::GetSubmitButtonValue(DWORD dwFieldID, DWORD* pdwAdjacentTo) {
    if (dwFieldID != FIELD_SUBMIT) return E_INVALIDARG;
    *pdwAdjacentTo = FIELD_STATUS;
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::GetSerialization(
    CREDENTIAL_PROVIDER_GET_SERIALIZATION_RESPONSE* pcpgsr,
    CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs,
    PWSTR* ppwszOptionalStatusText,
    CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {

    // Default safe response: tell LogonUI nothing happened, so the user can
    // immediately switch to the password tile or retry.
    *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    if (ppwszOptionalStatusText) *ppwszOptionalStatusText = nullptr;

    // All heavy work goes through PipeCall which has a hard 12s timeout and
    // wraps every Win32 call in error checks — there are no raw exceptions
    // that can escape here, so we rely on return codes rather than SEH
    // (SEH can't coexist with objects that have destructors in the same scope).
    std::wstring user, pw, dom;
    std::string action, err;
    bool ok = false;
    try {
        ok = RequestUnlock(user, pw, dom, action, err);
    } catch (...) {
        ok = false;
        err = "cpp-exception";
    }

    if (!ok) {
        m_status = L"Voice not recognised - use Sign-in options for the password";
        if (m_pEvents) m_pEvents->SetFieldString(this, FIELD_STATUS, m_status.c_str());
        *pcpsiOptionalStatusIcon = CPSI_ERROR;
        AllocStr(L"Voice unlock failed - use Sign-in options for the password", ppwszOptionalStatusText);
        return S_FALSE;  // LogonUI treats as non-fatal; user picks another tile
    }

    // ---- 无密码账户：不提交凭据，只注入一个回车 ----------------------------
    //
    // 为什么必须这样（两条都是实测/查证过的）：
    //   1) 空密码不能"提交"：LSA 会以 1327（空密码限制）拒绝，而且反复提交错误
    //      凭据会累计到账户锁定策略上（本机是 10 次锁 10 分钟）。
    //   2) 但无密码账户的解锁路径本来就是"按回车" —— 那是 Windows 自己的合法
    //      路径，我们只是替用户按下它。
    //
    // 为什么能在 CP 里做成：CP 由 LogonUI 加载，而 LogonUI 正运行在【安全桌面】上，
    // 所以这里的 SendInput 会落进当前输入桌面。普通用户会话里的进程做不到这件事
    // （这正是"不碰锁屏界面、也不装 SYSTEM 服务"就能静默解锁的关键）。
    if (action == "inject_enter") {
        INPUT keys[2] = {};
        keys[0].type = INPUT_KEYBOARD;
        keys[0].ki.wVk = VK_RETURN;
        keys[1].type = INPUT_KEYBOARD;
        keys[1].ki.wVk = VK_RETURN;
        keys[1].ki.dwFlags = KEYEVENTF_KEYUP;
        UINT sent = SendInput(2, keys, sizeof(INPUT));
        if (sent != 2) {
            m_status = L"Enter injection failed";
            if (m_pEvents) m_pEvents->SetFieldString(this, FIELD_STATUS, m_status.c_str());
            *pcpsiOptionalStatusIcon = CPSI_ERROR;
            return S_FALSE;
        }
        // 不返回任何凭据：让 LogonUI 自己按"回车"的默认语义完成登录。
        *pcpgsr = CPGSR_NO_CREDENTIAL_NOT_FINISHED;
        return S_FALSE;
    }

    HRESULT hr = E_FAIL;
    try {
        hr = KerbPackInteractiveUnlock(dom, user, pw, m_cpus, pcpcs);
    } catch (...) {
        hr = E_FAIL;
    }
    if (FAILED(hr)) {
        m_status = L"Credential packing failed";
        if (m_pEvents) m_pEvents->SetFieldString(this, FIELD_STATUS, m_status.c_str());
        *pcpsiOptionalStatusIcon = CPSI_ERROR;
        return S_FALSE;
    }
    pcpcs->clsidCredentialProvider = CLSID_VoiceCredentialProvider;
    *pcpgsr = CPGSR_RETURN_CREDENTIAL_FINISHED;
    return S_OK;
}

IFACEMETHODIMP VoiceCredential::ReportResult(NTSTATUS ntsStatus, NTSTATUS ntsSubstatus,
                                            PWSTR* ppwszOptionalStatusText,
                                            CREDENTIAL_PROVIDER_STATUS_ICON* pcpsiOptionalStatusIcon) {
    (void)ntsStatus; (void)ntsSubstatus;
    *ppwszOptionalStatusText = nullptr;
    *pcpsiOptionalStatusIcon = CPSI_NONE;
    return S_OK;
}

}  // namespace VoiceUnlock

