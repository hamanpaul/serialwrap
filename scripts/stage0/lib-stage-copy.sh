# shellcheck shell=bash
# win-pytest.sh 的 staging 複製：把 repo 中列於 manifest（NUL 分隔）的路徑複製到 stage。
# symlink 不解參考：在 stage 寫成「內容＝連結目標」的一般檔，與 Windows git（core.symlinks=false）
# checkout symlink 的形狀相同，staging 的 `git status` 才會是乾淨的（F-S19 provenance clean）。
stage_copy_repo() {
    local repo="$1" stage="$2" manifest="$3" path
    (
        cd "$repo" || exit 1
        while IFS= read -r -d '' path; do
            if [[ -L "$path" ]]; then
                mkdir -p -- "$stage/$(dirname -- "$path")"
                printf '%s' "$(readlink -- "$path")" > "$stage/$path"
                continue
            fi
            if [[ ! -e "$path" ]]; then
                continue
            fi
            cp --parents --target-directory="$stage" -- "$path"
        done < "$manifest"
    )
}
