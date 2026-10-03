#pragma once
#include <windows.h>
#include <string>

// Minimal blocking named-pipe client matching the Python FaceService protocol.
// Sends a single JSON request and reads a single JSON reply. Timeout is best-effort.
namespace VoiceUnlock {

// Returns true on success; on success, fills `response` with the raw reply JSON.
bool PipeCall(const std::wstring& pipeName,
              const std::string& requestJson,
              std::string& response,
              DWORD timeoutMs = 30000);

// Convenience: assemble {"cmd":"unlock"} and parse the reply.
// actionOut 里回的是"这次该怎么解锁"：
//   "credential"    —— 正常路径：填好 username/password/domain，由 CP 打包成
//                       KERB_INTERACTIVE_UNLOCK_LOGON 交给 LogonUI。
//   "inject_enter"  —— 本机账户【没有密码】，不能提交空凭据（LSA 会以 1327
//                       空密码限制拒绝，反复提交还可能触发账户锁定）。
//                       改为在安全桌面上注入一个回车，走 Windows 自己
//                       "按回车即登录"的合法路径。此时 username/password 为空。
// Any parse error -> returns false.
bool RequestUnlock(std::wstring& username,
                   std::wstring& password,
                   std::wstring& domain,
                   std::string& actionOut,
                   std::string& errorOut);

// 询问 agent：声纹票据是否已经备好（也就是 {"cmd":"ping"} 回复里的 "ticket" 字段）。
// 用途见 VoiceCredentialProvider：平时枚举 0 个凭据 —— 界面上完全没有我们，
// ShittimLogon 那套主题界面一个像素都不动；只有票据就绪时才让这个凭据出现，
// 并借 pbAutoLogonWithDefault 被 LogonUI 立刻自动提交。
bool PingTicketReady(bool& readyOut);

}  // namespace VoiceUnlock

