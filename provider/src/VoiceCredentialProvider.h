#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <atomic>
#include <thread>

namespace VoiceUnlock {

class VoiceCredential;

class VoiceCredentialProvider : public ICredentialProvider {
public:
    VoiceCredentialProvider();
    ~VoiceCredentialProvider();

    IFACEMETHODIMP QueryInterface(REFIID, void**) override;
    IFACEMETHODIMP_(ULONG) AddRef() override;
    IFACEMETHODIMP_(ULONG) Release() override;

    IFACEMETHODIMP SetUsageScenario(CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus, DWORD dwFlags) override;
    IFACEMETHODIMP SetSerialization(const CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION*) override;
    IFACEMETHODIMP Advise(ICredentialProviderEvents*, UINT_PTR) override;
    IFACEMETHODIMP UnAdvise() override;
    IFACEMETHODIMP GetFieldDescriptorCount(DWORD* pdwCount) override;
    IFACEMETHODIMP GetFieldDescriptorAt(DWORD dwIndex,
                                        CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR** ppcpfd) override;
    IFACEMETHODIMP GetCredentialCount(DWORD* pdwCount, DWORD* pdwDefault,
                                      BOOL* pbAutoLogonWithDefault) override;
    IFACEMETHODIMP GetCredentialAt(DWORD dwIndex, ICredentialProviderCredential** ppcpc) override;

private:
    void EnsureWatcher();
    void StopWatcher();
    void WatchLoop();

    LONG m_cRef;
    CREDENTIAL_PROVIDER_USAGE_SCENARIO m_cpus;
    VoiceCredential* m_pCred;

    // ---- 零界面占用所需的机制 -------------------------------------------
    // 平时枚举 0 个凭据 => 锁屏界面上【完全没有我们的东西】（ShittimLogon 的
    // 主题界面一个像素都不动）。只有当 agent 那边把"声纹票据"备好时，我们才
    // 通知 LogonUI 重新枚举，于是这一个凭据出现并因为 pbAutoLogonWithDefault
    // 被立刻自动提交 —— 用户什么都不用点。
    ICredentialProviderEvents* m_pEvents = nullptr;
    UINT_PTR m_adviseContext = 0;
    // ★ 事件接口必须经 GIT 封送：我们的看门线程不是 LogonUI 的线程，
    //   而 CP 注册的线程模型是 Apartment（STA）—— 跨线程直接调 COM 接口是错的。
    DWORD m_gitCookie = 0;
    std::atomic<bool> m_ticketReady{false};
    std::atomic<bool> m_stopThread{false};
    std::thread m_watch;
    bool m_watchStarted = false;
};

}  // namespace VoiceUnlock

