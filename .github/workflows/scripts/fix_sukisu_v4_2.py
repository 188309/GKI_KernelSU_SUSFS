"""Adapt the pinned SukiSU Ultra v4.2.0 builtin pre-exec hooks to SUSFS 2.3.

Run automatically by kernel_builder.py after its SUSFS patch has been applied.
The script deliberately rejects unexpected upstream changes rather than
producing an unverified boot image.
"""
from pathlib import Path
import re


class CompatibilityError(RuntimeError):
    pass


def _replace_one(text: str, old: str, new: str, filename: str) -> str:
    count = text.count(old)
    if count != 1:
        raise CompatibilityError(
            f"{filename}: expected one occurrence of {old.strip()!r}, found {count}; "
            "check the pinned SukiSU/SUSFS commits"
        )
    return text.replace(old, new, 1)


def fix_compat(work_dir: Path, config) -> None:
    """Fix the known android14-6.1 / SukiSU builtin ABI mismatch."""
    if (config.android_version, config.kernel_version) != ("android14", "6.1"):
        return

    root = Path(work_dir)
    ksu_file = root / "KernelSU/kernel/feature/sucompat.c"
    if not ksu_file.is_file():
        raise CompatibilityError(f"SukiSU source not found: {ksu_file}")
    ksu_text = ksu_file.read_text(encoding="utf-8")
    if not re.search(r"(?m)^bool\s+ksu_su_compat_enabled\b", ksu_text):
        raise CompatibilityError("Expected SukiSU builtin bool su-compat ABI is absent")
    if not re.search(r"(?m)^int\s+ksu_handle_execveat_sucompat\(", ksu_text):
        raise CompatibilityError("Expected SukiSU builtin pre-exec handler is absent")
    if re.search(r"(?m)^int\s+ksu_handle_post_execveat_sucompat\(", ksu_text):
        raise CompatibilityError("SukiSU now provides a post-exec handler; review this fix")

    # SUSFS's patch treats this bool as a static key in THREE files.  The
    # mismatch may link successfully but is unsafe at runtime, so fix all 3.
    common = root / "common"
    pending = {}
    for relative in ("fs/exec.c", "fs/open.c", "fs/stat.c"):
        path = common / relative
        if not path.is_file():
            raise CompatibilityError(f"Patched kernel source not found: {path}")
        source = path.read_text(encoding="utf-8")
        source = _replace_one(
            source,
            "extern struct static_key_true ksu_su_compat_enabled;",
            "extern bool ksu_su_compat_enabled;",
            relative,
        )
        source = _replace_one(
            source,
            "static_branch_likely(&ksu_su_compat_enabled)",
            "likely(ksu_su_compat_enabled)",
            relative,
        )

        if relative == "fs/exec.c":
            # SukiSU builtin v4.2.0 handles su-compat in the pre-exec hook.
            # Its handler's return value does NOT indicate a su session;
            # SUSFS's post-exec wrapper is absent from this SukiSU variant.
            source = _replace_one(
                source,
                "extern int ksu_handle_post_execveat_sucompat(int *fd, struct filename **filename_ptr, void *argv,\n"
                "\t\t\t\tvoid *envp, int *flags, int *retval);\n",
                "",
                relative,
            )
            source = _replace_one(
                source,
                "#ifdef CONFIG_KSU_SUSFS\n\tbool is_su_session = false;\n"
                "#endif // #ifdef CONFIG_KSU_SUSFS\n",
                "",
                relative,
            )
            source = _replace_one(
                source,
                "is_su_session = !ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);",
                "(void)ksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);",
                relative,
            )
            source = _replace_one(
                source,
                "is_su_session = !ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);",
                "(void)ksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);",
                relative,
            )
            source = _replace_one(
                source,
                "#ifdef CONFIG_KSU_SUSFS\n\tif (unlikely(is_su_session))\n"
                "\t\t(void)ksu_handle_post_execveat_sucompat(&fd, &filename, &argv, &envp, &flags, &retval);\n"
                "#endif // #ifdef CONFIG_KSU_SUSFS\n",
                "",
                relative,
            )
            if "ksu_handle_post_execveat_sucompat" in source or "is_su_session" in source:
                raise CompatibilityError("fs/exec.c still contains unsupported post-exec state")
        pending[path] = source

    # Only write files after every shape/ABI check has succeeded.
    for path, source in pending.items():
        path.write_text(source, encoding="utf-8")
        print(f"Fixed SukiSU builtin / SUSFS compat: {path.relative_to(root)}")
