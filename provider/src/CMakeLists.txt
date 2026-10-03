cmake_minimum_required(VERSION 3.20)
project(VoiceCredentialProvider LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(VoiceCredentialProvider SHARED
    dll.cpp
    ClassFactory.cpp
    VoiceCredentialProvider.cpp
    VoiceCredential.cpp
    PipeClient.cpp
    helpers.cpp
    VoiceCredentialProvider.def
)

target_compile_definitions(VoiceCredentialProvider PRIVATE
    UNICODE _UNICODE
    WIN32_LEAN_AND_MEAN
)

target_link_libraries(VoiceCredentialProvider PRIVATE
    credui
    secur32
    shlwapi
    ole32
    oleaut32
    advapi32
)

set_target_properties(VoiceCredentialProvider PROPERTIES
    OUTPUT_NAME "VoiceCredentialProvider"
    SUFFIX ".dll"
)


# Force UTF-8 source parsing. Without this MSVC uses the system codepage (936),
# where a non-ASCII comment can swallow the following line.
target_compile_options(VoiceCredentialProvider PRIVATE /utf-8)
