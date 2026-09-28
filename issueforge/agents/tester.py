import re
from typing import Optional, Tuple

from issueforge.config import settings
from issueforge.core.events import event_bus
from issueforge.core.models import AgentRole, EventType, Task, TestResult
from issueforge.core.sandbox import NativeSandbox, bytecode_free_env


class TesterAgent:
    """Discovers test suites, runs test commands, and parses results."""
    __test__ = False  # Prevent pytest from collecting this class

    def __init__(self, sandbox: NativeSandbox, task: Task):
        self.sandbox = sandbox
        self.task = task
        self.model = settings.tester_model

    def detect_test_command(self) -> Optional[str]:
        """Heuristically determine the best test command for the project."""
        # 1. Custom command in Makefile
        if self.sandbox.file_exists("Makefile"):
            try:
                makefile = self.sandbox.read_file("Makefile")
                if "test:" in makefile or "check:" in makefile:
                    return "make test"
            except Exception:
                pass

        # 2. Python projects
        has_tests_dir = self.sandbox.file_exists("tests")
        has_test_dir = self.sandbox.file_exists("test")
        has_python_config = (
            self.sandbox.file_exists("pytest.ini")
            or self.sandbox.file_exists("setup.py")
            or self.sandbox.file_exists("setup.cfg")
            or self.sandbox.file_exists("pyproject.toml")
            or self.sandbox.file_exists("requirements.txt")
        )

        all_files = self.sandbox.list_files(max_depth=4, max_files=200)
        has_test_files = any(
            f.endswith("_test.py") or "/test_" in f or f.startswith("test_")
            for f in all_files
        )

        if has_python_config or has_tests_dir or has_test_dir or has_test_files:
            if (
                self.sandbox.file_exists("pytest.ini")
                or self.sandbox.file_exists("setup.cfg")
                or self.sandbox.file_exists("pyproject.toml")
                or self.sandbox.file_exists("requirements.txt")
            ):
                return "python3 -m pytest -v"
            elif has_tests_dir:
                return "python3 -m unittest discover -s tests -p 'test_*.py'"
            elif has_test_dir:
                return "python3 -m unittest discover -s test -p 'test_*.py'"
            elif has_test_files:
                return "python3 -m unittest discover -p 'test_*.py'"
            else:
                return "python3 -m pytest -v"

        # 3. Node.js / JavaScript / TypeScript
        if self.sandbox.file_exists("package.json"):
            return "npm test"

        # 4. Rust
        if self.sandbox.file_exists("Cargo.toml"):
            return "cargo test"

        # 5. Go
        if self.sandbox.file_exists("go.mod"):
            return "go test ./..."

        # 6. Shell test scripts
        if self.sandbox.file_exists("run_tests.sh"):
            return "bash run_tests.sh"
        if self.sandbox.file_exists("test.sh"):
            return "bash test.sh"

        return None

    async def execute(self, custom_command: Optional[str] = None) -> TestResult:
        run_id = self.sandbox.run_id or "run-1"
        test_cmd = custom_command or self.detect_test_command()

        from issueforge.core.terminal_manager import terminal_manager
        term_session = terminal_manager.get_session(self.task.id, run_id)

        if not test_cmd:
            # No explicit test runner detected. Verify Python syntax & repo integrity.
            banner = (
                f"\r\n\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
                f"\x1b[1;36m[ISSUEFORGE AGENT SESSION]\x1b[0m 🧪 \x1b[1;33mTESTER\x1b[0m \x1b[90m(Workspace Syntax & Integrity Gate)\x1b[0m\r\n"
                f"\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
                f"\x1b[1;32missueforge@sandbox\x1b[0m:\x1b[1;34m~/{run_id}\x1b[0m$ python3 -m compileall -q .\r\n"
            )
            await term_session.broadcast_text(banner)

            await event_bus.emit_log(
                task_id=self.task.id,
                run_id=run_id,
                message="🧪 No automated test suite detected in repository. Running workspace syntax & compile integrity check...",
                role=AgentRole.TESTER.value,
                event_type=EventType.STEP
            )

            # Check for Python files and run compileall
            all_files = self.sandbox.list_files(max_depth=4, max_files=100)
            has_py = any(f.endswith(".py") for f in all_files)
            if has_py:
                check_result = await self.sandbox.run_command(
                "python3 -m compileall -q .", timeout=60, env_vars=bytecode_free_env(self.sandbox)
            )
                if not check_result.success:
                    err_out = check_result.stderr or check_result.stdout
                    await term_session.broadcast_text(f"\x1b[31m{err_out.strip()}\x1b[0m\r\n")
                    await term_session.broadcast_text(f"\r\n\x1b[1;31m✖ SYNTAX INTEGRITY FAILED\x1b[0m\r\n")
                    await event_bus.emit_log(
                        task_id=self.task.id,
                        run_id=run_id,
                        message=f"❌ Syntax verification failed:\n{err_out}",
                        role=AgentRole.TESTER.value,
                        event_type=EventType.TEST_RUN,
                        data={"passed": False, "total_tests": 0}
                    )
                    return TestResult(
                        passed=False,
                        total_tests=0,
                        passed_tests=0,
                        failed_tests=1,
                        test_runner="python3 -m compileall -q .",
                        stdout=check_result.stdout,
                        stderr=check_result.stderr,
                        failure_details=err_out
                    )

            await term_session.broadcast_text(f"\r\n\x1b[1;32m✔ SYNTAX & INTEGRITY VERIFIED (No test suite configured)\x1b[0m\r\n")
            await event_bus.emit_log(
                task_id=self.task.id,
                run_id=run_id,
                message="✅ Workspace syntax and integrity verified (no test suite defined).",
                role=AgentRole.TESTER.value,
                event_type=EventType.TEST_RUN,
                data={"passed": True, "total_tests": 0, "passed_tests": 0, "failed_tests": 0}
            )
            return TestResult(
                passed=True,
                total_tests=0,
                passed_tests=0,
                failed_tests=0,
                test_runner="Workspace Integrity Check (No test suite configured)",
                stdout="Syntax and workspace integrity verified. No test suite defined.",
                stderr="",
                failure_details=None
            )

        banner = (
            f"\r\n\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
            f"\x1b[1;36m[ISSUEFORGE AGENT SESSION]\x1b[0m 🧪 \x1b[1;33mTESTER\x1b[0m \x1b[90m(Automated Test Suite Runner)\x1b[0m\r\n"
            f"\x1b[1;35m═══════════════════════════════════════════════════════════════════════\x1b[0m\r\n"
            f"\x1b[1;32missueforge@sandbox\x1b[0m:\x1b[1;34m~/{run_id}\x1b[0m$ {test_cmd}\r\n"
        )
        await term_session.broadcast_text(banner)

        await event_bus.emit_log(
            task_id=self.task.id,
            run_id=run_id,
            message=f"🧪 Tester Agent running test command: `{test_cmd}`",
            role=AgentRole.TESTER.value,
            event_type=EventType.STEP,
            data={"command": test_cmd}
        )

        result = await self.sandbox.run_command(test_cmd, timeout=180)
        combined_output = (result.stdout + "\n" + result.stderr).strip()

        if result.stdout:
            await term_session.broadcast_text(f"{result.stdout.strip()}\r\n")
        if result.stderr:
            await term_session.broadcast_text(f"\x1b[31m{result.stderr.strip()}\x1b[0m\r\n")

        # Check for missing test directory or no tests collected
        is_missing_test_dir = (
            "Start directory is not importable" in combined_output
            or "collected 0 items" in combined_output
            or "ExitCode.NO_TESTS_COLLECTED" in combined_output
            or "no tests ran" in combined_output
            or "no tests were found" in combined_output
            or (result.exit_code == 5)
            or ("ERROR: file or directory not found" in combined_output)
        )

        # Parse test outcomes
        passed = result.success
        total_tests = 0
        passed_tests = 0
        failed_tests = 0
        failure_details = None

        # Regex heuristics for pytest
        # Handles any ordering: e.g. "=== 2 failed, 72 passed, 23 warnings in 65.42s ===" or "=== 74 passed in 39.37s ==="
        summary_line = None
        for line in reversed(combined_output.splitlines()):
            if "===" in line and any(k in line for k in ["passed", "failed", "error"]):
                summary_line = line
                break

        if summary_line:
            p_match = re.search(r"(\d+)\s+passed", summary_line)
            f_match = re.search(r"(\d+)\s+failed", summary_line)
            e_match = re.search(r"(\d+)\s+error", summary_line)
            p = int(p_match.group(1)) if p_match else 0
            f = int(f_match.group(1)) if f_match else 0
            e = int(e_match.group(1)) if e_match else 0
            passed_tests = p
            failed_tests = f + e
            total_tests = p + f + e
            passed = (result.exit_code == 0 and failed_tests == 0 and total_tests > 0)

        # Regex heuristics for unittest
        # e.g., "Ran 12 tests in 0.05s" and "FAILED (failures=1, errors=2)"
        unittest_ran = re.search(r"Ran (\d+) tests?", combined_output)
        if unittest_ran:
            total_tests = int(unittest_ran.group(1))
            if "OK" in combined_output:
                passed_tests = total_tests
                passed = True
            elif "FAILED" in combined_output:
                fail_match = re.search(r"failures=(\d+)", combined_output)
                err_match = re.search(r"errors=(\d+)", combined_output)
                f = int(fail_match.group(1)) if fail_match else 0
                e = int(err_match.group(1)) if err_match else 0
                failed_tests = max(f + e, 1)
                passed_tests = max(total_tests - failed_tests, 0)
                passed = False

        # If test command exited non-zero solely because 0 tests were found or directory doesn't exist
        if not passed and total_tests == 0 and is_missing_test_dir:
            # Verify syntax to ensure code wasn't broken
            check_result = await self.sandbox.run_command(
                "python3 -m compileall -q .", timeout=60, env_vars=bytecode_free_env(self.sandbox)
            )
            if check_result.success:
                passed = True
                failed_tests = 0
                await event_bus.emit_log(
                    task_id=self.task.id,
                    message="ℹ️ Test runner found 0 test files in directory. Workspace syntax verified successfully.",
                    role=AgentRole.TESTER.value,
                    event_type=EventType.LOG
                )

        if not passed:
            lines = combined_output.split("\n")
            failure_details = "\n".join(lines[-30:])

        if passed and total_tests == 0:
            summary_msg = "✅ Verification passed (0 tests in suite / syntax verified)"
        elif passed:
            summary_msg = f"✅ All tests passed! ({passed_tests}/{total_tests})"
        else:
            summary_msg = f"❌ Tests failed! ({failed_tests} failed out of {total_tests or 'all'})"

        outcome_ansi = (
            f"\r\n\x1b[1;32m✔ TEST RUNNER PASSED: {passed_tests}/{total_tests} tests passing\x1b[0m\r\n"
            if passed
            else f"\r\n\x1b[1;31m✖ TEST RUNNER FAILED: {failed_tests} failed out of {total_tests or 'all'}\x1b[0m\r\n"
        )
        await term_session.broadcast_text(outcome_ansi)

        run_id = self.sandbox.run_id or "run-1"
        await event_bus.emit_log(
            task_id=self.task.id,
            run_id=run_id,
            message=f"🧪 Test Results: {summary_msg}",
            role=AgentRole.TESTER.value,
            event_type=EventType.TEST_RUN,
            data={
                "passed": passed,
                "passed_tests": passed_tests,
                "failed_tests": failed_tests,
                "total_tests": total_tests,
                "output_preview": combined_output[-500:]
            }
        )

        # Write TEST_RESULTS.md into workspace for Reviewer agent handoff
        try:
            results_md = f"""# Automated Test Verification Results
- **Test Runner**: `{test_cmd}`
- **Outcome**: `{'PASSED' if passed else 'FAILED'}`
- **Metrics**: {passed_tests}/{total_tests} passed, {failed_tests} failed

## Output Summary
```
{combined_output[-1500:]}
```
"""
            (self.sandbox.workspace_path / "TEST_RESULTS.md").write_text(results_md, encoding="utf-8")
        except Exception:
            pass

        return TestResult(
            passed=passed,
            total_tests=total_tests,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            test_runner=test_cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            failure_details=failure_details
        )
