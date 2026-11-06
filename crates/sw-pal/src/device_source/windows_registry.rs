//! Windows 原生登錄讀取。

use crate::device_source::windows_model::RegistryView;
use crate::traits::{windows_win32_error, PalResult};
use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegEnumKeyExW, RegEnumValueW, RegOpenKeyExW, RegQueryValueExW, HKEY,
    HKEY_LOCAL_MACHINE, KEY_READ, REG_SZ,
};

const ERROR_FILE_NOT_FOUND: u32 = 2;
const ERROR_PATH_NOT_FOUND: u32 = 3;
const ERROR_NO_MORE_ITEMS: u32 = 259;
const ERROR_MORE_DATA: u32 = 234;
const ERROR_INVALID_PARAMETER: u32 = 87;
const MAX_VALUE_NAME: usize = 16_384;

/// `RegOpenKeyExW`、`RegEnumValueW` 與 `RegQueryValueExW` 的原生實作。
#[derive(Debug, Clone, Copy, Default)]
pub struct WinRegistryView;

impl RegistryView for WinRegistryView {
    fn enum_values(&self, key: &str) -> PalResult<Vec<(String, String)>> {
        let handle = match open_key(key) {
            Ok(handle) => handle,
            Err(code) if is_missing(code) => return Ok(Vec::new()),
            Err(code) => return Err(windows_win32_error(code)),
        };
        let mut rows = Vec::new();
        let mut index = 0u32;
        loop {
            match enum_value(handle.0, index)? {
                Some((name, REG_SZ, data)) => {
                    if let Some(value) = decode_registry_string(&data) {
                        rows.push((name, value));
                    }
                    index = index.saturating_add(1);
                }
                Some(_) => index = index.saturating_add(1),
                None => break,
            }
        }
        Ok(rows)
    }

    fn enum_subkeys(&self, key: &str) -> PalResult<Vec<String>> {
        let handle = match open_key(key) {
            Ok(handle) => handle,
            Err(code) if is_missing(code) => return Ok(Vec::new()),
            Err(code) => return Err(windows_win32_error(code)),
        };
        let mut names = Vec::new();
        let mut index = 0u32;
        loop {
            let mut name = vec![0u16; MAX_VALUE_NAME];
            let mut name_len = name.len() as u32;
            // SAFETY: handle 已開啟；名稱緩衝區與長度都是有效的可寫儲存空間。
            let result = unsafe {
                RegEnumKeyExW(
                    handle.0,
                    index,
                    name.as_mut_ptr(),
                    &mut name_len,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            match result {
                ERROR_NO_MORE_ITEMS => break,
                ERROR_MORE_DATA => return Err(windows_win32_error(result)),
                0 => {
                    name.truncate(name_len as usize);
                    names.push(String::from_utf16_lossy(&name));
                    index = index.saturating_add(1);
                }
                code => return Err(windows_win32_error(code)),
            }
        }
        Ok(names)
    }

    fn query_string(&self, key: &str, name: &str) -> PalResult<Option<String>> {
        let handle = match open_key(key) {
            Ok(handle) => handle,
            Err(code) if is_missing(code) => return Ok(None),
            Err(code) => return Err(windows_win32_error(code)),
        };
        let name_wide = wide_null(name);
        let mut value_type = 0u32;
        let mut byte_len = 0u32;
        // SAFETY: 值名稱以 NUL 結尾；data 為 null 時只查詢所需位元組數。
        let result = unsafe {
            RegQueryValueExW(
                handle.0,
                name_wide.as_ptr(),
                std::ptr::null_mut(),
                &mut value_type,
                std::ptr::null_mut(),
                &mut byte_len,
            )
        };
        if is_missing(result) {
            return Ok(None);
        }
        if result != 0 {
            return Err(windows_win32_error(result));
        }
        if value_type != REG_SZ {
            return Ok(None);
        }
        let mut data = vec![0u8; byte_len as usize];
        if byte_len > 0 {
            // SAFETY: data 的大小符合探測呼叫回報的需求。
            let result = unsafe {
                RegQueryValueExW(
                    handle.0,
                    name_wide.as_ptr(),
                    std::ptr::null_mut(),
                    &mut value_type,
                    data.as_mut_ptr(),
                    &mut byte_len,
                )
            };
            if is_missing(result) {
                return Ok(None);
            }
            if result != 0 {
                return Err(windows_win32_error(result));
            }
        }
        Ok(decode_registry_string(&data))
    }
}

struct RegistryHandle(HKEY);

impl Drop for RegistryHandle {
    fn drop(&mut self) {
        // SAFETY: 此包裝器擁有 RegOpenKeyExW 回傳的控制代碼。
        unsafe { RegCloseKey(self.0) };
    }
}

fn open_key(key: &str) -> Result<RegistryHandle, u32> {
    let Some(relative) = strip_hklm(key) else {
        return Err(ERROR_INVALID_PARAMETER);
    };
    let relative = wide_null(relative);
    let mut handle = std::ptr::null_mut();
    // SAFETY: 路徑以 NUL 結尾，handle 是有效的輸出位置。
    let result = unsafe {
        RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            relative.as_ptr(),
            0,
            KEY_READ,
            &mut handle,
        )
    };
    if result == 0 {
        Ok(RegistryHandle(handle))
    } else {
        Err(result)
    }
}

fn strip_hklm(key: &str) -> Option<&str> {
    let (hive, relative) = key.split_once('\\')?;
    (hive.eq_ignore_ascii_case("HKLM") || hive.eq_ignore_ascii_case("HKEY_LOCAL_MACHINE"))
        .then_some(relative)
}

fn enum_value(handle: HKEY, index: u32) -> PalResult<Option<(String, u32, Vec<u8>)>> {
    let mut name = vec![0u16; MAX_VALUE_NAME];
    let mut byte_len = 0u32;
    let mut value_type = 0u32;
    let mut name_len = name.len() as u32;
    // SAFETY: data 為 null 時只查詢所需大小；名稱緩衝區與長度可寫入。
    let result = unsafe {
        RegEnumValueW(
            handle,
            index,
            name.as_mut_ptr(),
            &mut name_len,
            std::ptr::null_mut(),
            &mut value_type,
            std::ptr::null_mut(),
            &mut byte_len,
        )
    };
    if result == ERROR_NO_MORE_ITEMS {
        return Ok(None);
    }
    if result != 0 && result != ERROR_MORE_DATA {
        return Err(windows_win32_error(result));
    }

    let mut data = vec![0u8; byte_len as usize];
    loop {
        name_len = name.len() as u32;
        let mut capacity = data.len() as u32;
        // SAFETY: 名稱與資料緩衝區大小符合提供的容量。
        let result = unsafe {
            RegEnumValueW(
                handle,
                index,
                name.as_mut_ptr(),
                &mut name_len,
                std::ptr::null_mut(),
                &mut value_type,
                data.as_mut_ptr(),
                &mut capacity,
            )
        };
        if result == ERROR_MORE_DATA {
            let next = capacity.max((data.len() as u32).saturating_mul(2)).max(2);
            data.resize(next as usize, 0);
            continue;
        }
        if result == ERROR_NO_MORE_ITEMS {
            return Ok(None);
        }
        if result != 0 {
            return Err(windows_win32_error(result));
        }
        name.truncate(name_len as usize);
        data.truncate(capacity as usize);
        return Ok(Some((String::from_utf16_lossy(&name), value_type, data)));
    }
}

fn decode_registry_string(data: &[u8]) -> Option<String> {
    let units: Vec<u16> = data
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .take_while(|unit| *unit != 0)
        .collect();
    Some(String::from_utf16_lossy(&units))
}

fn wide_null(value: &str) -> Vec<u16> {
    OsStr::new(value).encode_wide().chain(Some(0)).collect()
}

fn is_missing(code: u32) -> bool {
    code == ERROR_FILE_NOT_FOUND || code == ERROR_PATH_NOT_FOUND
}
