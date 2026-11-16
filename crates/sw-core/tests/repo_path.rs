mod common;

#[test]
fn repo_path_prefers_sw_test_data_dir() {
    let data_dir = tempfile::tempdir().unwrap();
    let previous = std::env::var_os("SW_TEST_DATA_DIR");
    unsafe {
        std::env::set_var("SW_TEST_DATA_DIR", data_dir.path());
    }

    let resolved = common::repo_path("contracts/golden/p7/sentinel.ndjson");

    if let Some(previous) = previous {
        unsafe {
            std::env::set_var("SW_TEST_DATA_DIR", previous);
        }
    } else {
        unsafe {
            std::env::remove_var("SW_TEST_DATA_DIR");
        }
    }

    assert!(
        resolved.starts_with(data_dir.path()),
        "repo_path 應落在 SW_TEST_DATA_DIR 下；實際為 {}",
        resolved.display()
    );
}
