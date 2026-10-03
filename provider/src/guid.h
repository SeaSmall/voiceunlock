#pragma once
#include <initguid.h>

// {7C4E1A62-9D3B-4F58-A1E7-5B2C8D94F013}
// Project-specific CLSID. The reference project (windows-face-unlock) publishes
// its own CLSID; reusing it would collide with other installs of that project.
//
// Keep this file pure ASCII: MSVC parses sources in the system codepage
// (936 here) unless there is a BOM or /utf-8 is passed, and a non-ASCII
// comment can swallow the next line and break the DEFINE_GUID below.
DEFINE_GUID(CLSID_VoiceCredentialProvider,
    0x7c4e1a62, 0x9d3b, 0x4f58, 0xa1, 0xe7, 0x5b, 0x2c, 0x8d, 0x94, 0xf0, 0x13);
