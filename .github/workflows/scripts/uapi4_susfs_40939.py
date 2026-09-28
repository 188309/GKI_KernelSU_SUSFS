"""Pinned SukiSU builtin 40939/UAPI4 + SUSFS v2.3 integration for TB710FU build.

Copy this file into .github/workflows/scripts/ of 188309/GKI_KernelSU_SUSFS.
This is a candidate build integration, NOT a tested flashable kernel.
"""

from pathlib import Path
import re
import subprocess

SUKISU_COMMIT = "b20dee702035af09cb2ecb5f35443bbc1747f3e6"
SUSFS_COMMIT = "24743360ea08d98f6ad72b856851abed8de5854f"
UAPI4_PATCH_COMMIT = "12e3b460b69e458caa24a6e9cb1b8fb18bbc78df"
UAPI4_PATCH_BLOB = "84bdf1d32eeb9b25126ba4dd4c7e5c7f0201518e"
UAPI4_PATCH_URL = (
    "https://raw.githubusercontent.com/LingLuo17/AnyKernel3/"
    + UAPI4_PATCH_COMMIT
    + "/scripts/ksu_uapi_sync/builtin-uapi4.patch"
)

# The exact five kernel-source blobs which the backport was authored for.
ORIGINAL_BLOBS = {
    "kernel/include/uapi/supercall.h": "bfdc1d144d1e0f91a72dedc00520ba8991474a72",
    "kernel/supercall/dispatch.c": "c1d3abaf85786766b90d9e9d557dc55979674d36",
    "kernel/supercall/internal.h": "4ab6d925ebc49f44f2e13cafa39ccd6842f681a2",
    "kernel/supercall/supercall.c": "c259c92e1cad30de6b5de7bfa49f3ec1c46136e2",
    "kernel/supercall/supercall.h": "50a35a0fc93745bce19a161f043b436c23032cb5",
}


def _run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _replace_once(text, old, new, where):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{where}: expected one copy of {old[:90]!r}; found {count}")
    return text.replace(old, new, 1)


def _check_config(config):
    if (config.android_version, config.kernel_version, config.sub_level) != ("android14", "6.1", "128"):
        raise RuntimeError("This candidate is only for android14-6.1.128")
    if config.kernelsu_commit != SUKISU_COMMIT:
        raise RuntimeError("Wrong SukiSU commit: " + str(config.kernelsu_commit))
    if config.susfs_commit != SUSFS_COMMIT:
        raise RuntimeError("Wrong SUSFS commit: " + str(config.susfs_commit))


def _git_blob(path):
    return _run("git", "hash-object", str(path))


def prepare_uapi4(work_dir: Path, config):
    """Call right AFTER KernelBuilder.add_kernelsu, before SUSFS patching."""
    _check_config(config)
    root = Path(work_dir)
    ksu = root / "KernelSU"
    susfs = root.parent / "susfs4ksu"

    if _run("git", "-C", str(ksu), "rev-parse", "HEAD") != SUKISU_COMMIT:
        raise RuntimeError("SukiSU checkout failed; stop the build")
    if _run("git", "-C", str(susfs), "rev-parse", "HEAD") != SUSFS_COMMIT:
        raise RuntimeError("SUSFS checkout failed; stop the build")
    for relative, expected in ORIGINAL_BLOBS.items():
        path = ksu / relative
        actual = _git_blob(path)
        if actual != expected:
            raise RuntimeError(f"Unexpected source {relative}: {actual}; expected {expected}")

    patch_path = root / "sukisu-builtin-uapi4.patch"
    _run("curl", "-fsSL", UAPI4_PATCH_URL, "-o", str(patch_path))
    if _git_blob(patch_path) != UAPI4_PATCH_BLOB:
        raise RuntimeError("UAPI4 patch was changed or corrupted")

    _run("git", "apply", "--check", str(patch_path), cwd=ksu)
    _run("git", "apply", str(patch_path), cwd=ksu)

    uapi = (ksu / "kernel/include/uapi/supercall.h").read_text(encoding="utf-8")
    if "DECLARE(__u32, KERNEL_SU_UAPI_VERSION, 4);" not in uapi:
        raise RuntimeError("UAPI4 backport did not take effect")
    driver = (ksu / "kernel/supercall/supercall.c").read_text(encoding="utf-8")
    if "int ksu_install_su_fd(void)" not in driver:
        raise RuntimeError("Scoped SU-session fd implementation is missing")

    makefile = ksu / "kernel/Makefile"
    text = makefile.read_text(encoding="utf-8")
    text, n = re.subn(r"(?m)^KSU_VERSION[ \t]*:=.*$", "KSU_VERSION := 40939", text)
    if n != 1:
        raise RuntimeError(f"Unexpected KSU_VERSION definitions: {n}")
    makefile.write_text(text, encoding="utf-8")
    print("PREPARED: pinned SukiSU builtin / UAPI4 backport / KSU_VERSION=40939")


def fix_compat_uapi4(work_dir: Path, config):
    """Call right AFTER apply_susfs_patches; replace old fix_compat call."""
    _check_config(config)
    root = Path(work_dir)
    ksu = root / "KernelSU/kernel/feature/sucompat.c"
    original = ksu.read_text(encoding="utf-8")
    if not re.search(r"(?m)^bool\s+ksu_su_compat_enabled\b", original):
        raise RuntimeError("SukiSU sucompat bool ABI changed")
    if not re.search(r"(?m)^int\s+ksu_handle_execveat_sucompat\(", original):
        raise RuntimeError("SukiSU execveat hook ABI changed")

    # The builtin hook returns 0 for *both* ordinary exec and successful su.
    # Return 1 only after this precise su path has gained its root profile.
    old_end = (
        "    ksu_sulog_emit_pending(pending_sucompat, ret, GFP_KERNEL);\n"
        "    return 0;\n"
        "}\n\n#ifdef KSU_COMPAT_USE_STATIC_KEY\n"
    )
    new_end = (
        "    ksu_sulog_emit_pending(pending_sucompat, ret, GFP_KERNEL);\n"
        "    return ret == 0 ? 1 : 0; /* successful su session */\n"
        "}\n\n#ifdef KSU_COMPAT_USE_STATIC_KEY\n"
    )
    pending = {ksu: _replace_once(original, old_end, new_end, str(ksu))}

    # The SUSFS kernel patch incorrectly declares this boolean as a static key
    # in all three places. Keep strict one-to-one replacement checks.
    common = root / "common"
    for relative in ("fs/exec.c", "fs/open.c", "fs/stat.c"):
        path = common / relative
        source = path.read_text(encoding="utf-8")
        source = _replace_once(
            source, "extern struct static_key_true ksu_su_compat_enabled;",
            "extern bool ksu_su_compat_enabled;", relative,
        )
        source = _replace_once(
            source, "static_branch_likely(&ksu_su_compat_enabled)",
            "likely(ksu_su_compat_enabled)", relative,
        )
        if relative == "fs/exec.c":
            source = _replace_once(
                source,
                "extern int ksu_handle_post_execveat_sucompat(int *fd, struct filename **filename_ptr, void *argv,\n"
                "\t\t\t\tvoid *envp, int *flags, int *retval);\n",
                "extern int ksu_install_su_fd(void);\n", relative,
            )
            source = _replace_once(
                source,
                "is_su_session = !ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);",
                "is_su_session = ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags) > 0;",
                relative,
            )
            source = _replace_once(
                source,
                "is_su_session = !ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);",
                "is_su_session = ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags) > 0;",
                relative,
            )
            source = _replace_once(
                source,
                "#ifdef CONFIG_KSU_SUSFS\n"
                "\tif (unlikely(is_su_session))\n"
                "\t\t(void)ksu_handle_post_execveat_sucompat(&fd, &filename, &argv, &envp, &flags, &retval);\n"
                "#endif // #ifdef CONFIG_KSU_SUSFS\n",
                "#ifdef CONFIG_KSU_SUSFS\n"
                "\tif (unlikely(is_su_session && retval == 0))\n"
                "\t\t(void)ksu_install_su_fd();\n"
                "#endif // #ifdef CONFIG_KSU_SUSFS\n",
                relative,
            )
            if "ksu_handle_post_execveat_sucompat" in source:
                raise RuntimeError("Old unsupported post-exec helper remains")
            if source.count("ksu_install_su_fd();") != 1:
                raise RuntimeError("SU session fd must be installed exactly once")
        pending[path] = source

    # Reject any partially applied patch before writing any source changes.
    rejects = list(ksu.parent.parent.rglob("*.rej")) + list(common.rglob("*.rej"))
    if rejects:
        raise RuntimeError("Patching left rejects: " + ", ".join(map(str, rejects[:8])))

    for path, source in pending.items():
        path.write_text(source, encoding="utf-8")
    print("PATCHED: SUSFS 2.3 / builtin hooks / real scoped UAPI4 SU session")


def verify_candidate(work_dir: Path, config):
    """Fail the build if any expected UAPI/SUSFS source or config is missing.

    This is a source/configuration gate, NOT device ABI or boot validation.
    Call after configure_kernel() and after other source patching.
    """
    _check_config(config)
    root = Path(work_dir)
    ksu = root / "KernelSU"
    common = root / "common"
    checks = {
        ksu / "kernel/include/uapi/supercall.h":
            "DECLARE(__u32, KERNEL_SU_UAPI_VERSION, 4);",
        ksu / "kernel/supercall/supercall.c": "int ksu_install_su_fd(void)",
        ksu / "kernel/feature/sucompat.c":
            "return ret == 0 ? 1 : 0; /* successful su session */",
        common / "fs/exec.c": "(void)ksu_install_su_fd();",
        common / "arch/arm64/configs/gki_defconfig": "CONFIG_KSU_SUSFS=y",
        common / "include/linux/susfs.h": "SUSFS_VERSION",
    }
    for path, expected in checks.items():
        if not path.is_file():
            raise RuntimeError(f"Required UAPI4/SUSFS file is absent: {path}")
        if expected not in path.read_text(encoding="utf-8"):
            raise RuntimeError(f"Required UAPI4/SUSFS content is absent: {path}: {expected}")

    susfs_header = (common / "include/linux/susfs.h").read_text(encoding="utf-8")
    if '#define SUSFS_VERSION "v2.3.0"' not in susfs_header:
        raise RuntimeError("SUSFS is not v2.3.0")

    cfg = (common / "arch/arm64/configs/gki_defconfig").read_text(encoding="utf-8")
    for flag in ("CONFIG_KSU=y", "CONFIG_KSU_SUSFS=y"):
        if re.search(rf"(?m)^{re.escape(flag)}$", cfg) is None:
            raise RuntimeError(f"Missing kernel config: {flag}")

    makefile = (ksu / "kernel/Makefile").read_text(encoding="utf-8")
    if re.search(r"(?m)^KSU_VERSION\s*:=\s*40939\s*$", makefile) is None:
        raise RuntimeError("KSU_VERSION is not pinned to 40939")

    rejects = sorted(root.rglob("*.rej"))
    if rejects:
        raise RuntimeError("Unresolved patch rejects: " + ", ".join(map(str, rejects[:8])))

    print("VERIFIED: candidate source/config has 40939 / UAPI4 / SUSFS; NOT device-tested")
