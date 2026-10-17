#!/usr/bin/env python3
"""Private pipe-only Keychain adapter. Never put login state in argv or files."""
import base64
import ctypes as c
import json
import sys

SERVICE = "com.openai.opensms.browser.auth-state"
ACCOUNT = "adminsms.smsoug.com"


def main(service=SERVICE, account=ACCOUNT):
    if sys.platform != "darwin" or sys.argv[1:] not in (["get"], ["put"]):
        raise RuntimeError("KEYCHAIN_UNAVAILABLE")
    cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    sec = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
    ptr = c.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ptr, c.c_char_p, c.c_uint32]
    cf.CFStringCreateWithCString.restype = ptr
    cf.CFDataCreate.argtypes = [ptr, ptr, c.c_long]
    cf.CFDataCreate.restype = ptr
    cf.CFDataGetLength.argtypes = [ptr]
    cf.CFDataGetLength.restype = c.c_long
    cf.CFDataGetBytePtr.argtypes = [ptr]
    cf.CFDataGetBytePtr.restype = ptr
    cf.CFDictionaryCreateMutable.argtypes = [ptr, c.c_long, ptr, ptr]
    cf.CFDictionaryCreateMutable.restype = ptr
    cf.CFDictionarySetValue.argtypes = [ptr, ptr, ptr]
    cf.CFRelease.argtypes = [ptr]
    for name, args in (("SecItemCopyMatching", [ptr, c.POINTER(ptr)]),
                       ("SecItemAdd", [ptr, ptr]), ("SecItemUpdate", [ptr, ptr])):
        function = getattr(sec, name)
        function.argtypes = args
        function.restype = c.c_int32

    allocated = []

    def symbol(library, name):
        return ptr.in_dll(library, name).value

    def string(value):
        result = cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
        allocated.append(result)
        return result

    def dictionary(values):
        keys = c.addressof(c.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        vals = c.addressof(c.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        result = cf.CFDictionaryCreateMutable(None, 0, keys, vals)
        allocated.append(result)
        for key, value in values.items():
            cf.CFDictionarySetValue(result, symbol(sec, key), value)
        return result

    try:
        # Explicitly non-synchronizable. Locked/interactive Keychain access fails
        # closed instead of opening a password dialog during a scheduled job.
        common = {"kSecClass": symbol(sec, "kSecClassGenericPassword"),
                  "kSecAttrService": string(service), "kSecAttrAccount": string(account),
                  "kSecAttrSynchronizable": symbol(cf, "kCFBooleanFalse"),
                  "kSecUseAuthenticationUI": symbol(sec, "kSecUseAuthenticationUIFail")}
        if sys.argv[1] == "get":
            query = dictionary({**common, "kSecReturnData": symbol(cf, "kCFBooleanTrue"),
                                "kSecMatchLimit": symbol(sec, "kSecMatchLimitOne")})
            result = ptr()
            code = sec.SecItemCopyMatching(query, c.byref(result))
            if code == -25300:
                print(json.dumps({"status": "missing"}))
                return
            if code or not result.value:
                raise RuntimeError("KEYCHAIN_UNAVAILABLE")
            allocated.append(result.value)
            length = cf.CFDataGetLength(result)
            if not 0 < length <= 1048576:
                raise RuntimeError("KEYCHAIN_STATE_INVALID")
            raw = c.string_at(cf.CFDataGetBytePtr(result), length)
            # Only the calling Node process consumes this private pipe.
            print(json.dumps({"status": "found", "data": base64.b64encode(raw).decode()}))
        else:
            raw = sys.stdin.buffer.read(1048577)
            if not 0 < len(raw) <= 1048576:
                raise RuntimeError("KEYCHAIN_STATE_INVALID")
            buffer = c.create_string_buffer(raw)
            data = cf.CFDataCreate(None, c.cast(buffer, ptr), len(raw))
            allocated.append(data)
            query = dictionary(common)
            code = sec.SecItemUpdate(query, dictionary({"kSecValueData": data}))
            if code == -25300:
                code = sec.SecItemAdd(dictionary({**common, "kSecValueData": data}), None)
            if code:
                raise RuntimeError("KEYCHAIN_UNAVAILABLE")
            print(json.dumps({"status": "saved"}))
    finally:
        for value in reversed(allocated):
            cf.CFRelease(value)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"status": "failed", "error": "OPENSMS_AUTH_KEYCHAIN_UNAVAILABLE"}))
        sys.exit(2)
