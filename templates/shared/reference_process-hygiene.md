---
name: reference_process-hygiene
description: Stop processes by pid, stop their children too, and never start a tracked job that cannot end
shareable: true
kind: rule
---
- `pkill -f <pattern>` also matches your own shell when the pattern is in its command line, and kills it mid-script. Kill by pid (a listener's from `ss -ltnp`), then prove the port is dead before starting a replacement.
- `ps -C python3` misses venv interpreters; a stale server left on a port keeps serving old code after a reinstall.
- Children outlive their parent: a killed wrapper leaves its `ssh ... tail -F` running. Check `pgrep -P <pid>` and stop the children or the process group. Never kill by pattern on a box running builds.
- A tracked job whose command never exits (`tail -F`) leaks forever: run a bounded command that exits when the work does.
