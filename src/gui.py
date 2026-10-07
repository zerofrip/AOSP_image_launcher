"""Tk frontend. Calls Python APIs directly — never assembles launcher.py CLI strings."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capabilities import detect_host, discover_tools  # noqa: E402
from command_builder import build_launch_plan, format_command_redacted  # noqa: E402
from errors import LauncherError  # noqa: E402
from models import LaunchOptions  # noqa: E402
from product_out import inventory_product_out, resolve_product_out  # noqa: E402


class StartLoaderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("StartLoader — AOSP x86_64 PRODUCT_OUT")
        self.root.geometry("840x640")
        self.product_out_var = tk.StringVar()
        self.backend_var = tk.StringVar(value="auto")
        self.accel_var = tk.StringVar(value="auto")
        self.memory_var = tk.IntVar(value=4096)
        self.cpus_var = tk.IntVar(value=max(1, min(4, os.cpu_count() or 1)))
        self.adb_port_var = tk.IntVar(value=5555)
        self.headless_var = tk.BooleanVar(value=False)
        self.read_only_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Select a PRODUCT_OUT directory.")
        self.bootable = False
        self.plan = None
        self.product = None
        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._build()
        self.root.after(200, self._drain_logs)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        menubar = tk.Menu(self.root)
        extras = tk.Menu(menubar, tearoff=0)
        extras.add_command(
            label="Custom ROM Creator (legacy mock — non-functional)",
            command=self.launch_rom_creator,
        )
        menubar.add_cascade(label="Extras", menu=extras)
        self.root.config(menu=menubar)

        top = tk.Frame(self.root, padx=12, pady=8)
        top.pack(fill=tk.X)
        tk.Label(top, text="PRODUCT_OUT:").pack(side=tk.LEFT)
        tk.Entry(top, textvariable=self.product_out_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6
        )
        tk.Button(top, text="Browse…", command=self.browse_product_out).pack(side=tk.LEFT)

        opts = tk.Frame(self.root, padx=12)
        opts.pack(fill=tk.X, pady=2)
        tk.Label(opts, text="backend").pack(side=tk.LEFT)
        tk.OptionMenu(opts, self.backend_var, "auto", "emulator", "qemu").pack(side=tk.LEFT)
        tk.Label(opts, text="accel").pack(side=tk.LEFT, padx=(8, 0))
        tk.OptionMenu(opts, self.accel_var, "auto", "whpx", "kvm", "tcg").pack(side=tk.LEFT)
        tk.Label(opts, text="memory").pack(side=tk.LEFT, padx=(8, 0))
        tk.Spinbox(opts, from_=1, to=65536, textvariable=self.memory_var, width=6).pack(side=tk.LEFT)
        tk.Label(opts, text="cpus").pack(side=tk.LEFT, padx=(8, 0))
        tk.Spinbox(opts, from_=1, to=64, textvariable=self.cpus_var, width=3).pack(side=tk.LEFT)
        tk.Label(opts, text="adb").pack(side=tk.LEFT, padx=(8, 0))
        tk.Spinbox(opts, from_=1, to=65535, textvariable=self.adb_port_var, width=6).pack(side=tk.LEFT)
        tk.Checkbutton(opts, text="headless", variable=self.headless_var).pack(side=tk.LEFT, padx=(8, 0))
        tk.Checkbutton(opts, text="read-only", variable=self.read_only_var).pack(side=tk.LEFT)

        buttons = tk.Frame(self.root, padx=12)
        buttons.pack(fill=tk.X, pady=4)
        tk.Button(buttons, text="Diagnose", command=self.run_diagnose).pack(side=tk.LEFT, padx=2)
        tk.Button(buttons, text="Preview", command=self.preview_command).pack(side=tk.LEFT, padx=2)
        self.launch_btn = tk.Button(
            buttons, text="Launch", command=self.launch, state=tk.DISABLED
        )
        self.launch_btn.pack(side=tk.LEFT, padx=2)
        tk.Button(buttons, text="Stop", command=self.stop).pack(side=tk.LEFT, padx=2)
        tk.Label(self.root, textvariable=self.status_var, anchor="w", padx=12).pack(fill=tk.X)

        self.log = scrolledtext.ScrolledText(self.root, height=28, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

    def browse_product_out(self) -> None:
        chosen = filedialog.askdirectory(title="Select AOSP PRODUCT_OUT")
        if chosen:
            self.product_out_var.set(chosen)
            self.run_diagnose()

    def _options(self) -> LaunchOptions:
        return LaunchOptions(
            backend=self.backend_var.get() or "auto",
            accel=self.accel_var.get() or "auto",
            memory_mb=int(self.memory_var.get()),
            cpus=int(self.cpus_var.get()),
            adb_port=int(self.adb_port_var.get()),
            headless=bool(self.headless_var.get()),
            read_only=bool(self.read_only_var.get()),
        )

    def _inspect(self):
        path = self.product_out_var.get().strip()
        if not path:
            messagebox.showwarning("PRODUCT_OUT", "Select a PRODUCT_OUT directory first.")
            return None
        resolved = resolve_product_out(path)
        product = inventory_product_out(resolved)
        caps = discover_tools(product_out=product.product_out, host=detect_host())
        plan = build_launch_plan(product, self._options(), caps)
        return product, caps, plan

    def run_diagnose(self) -> None:
        try:
            from launcher import format_diagnose_report

            inspected = self._inspect()
            if inspected is None:
                return
            product, caps, plan = inspected
            self.product = product
            self.plan = plan
            self.bootable = bool(plan.spec.bootable and plan.spec.argv)
            self.launch_btn.config(state=tk.NORMAL if self.bootable else tk.DISABLED)
            report = format_diagnose_report(product, caps, plan)
            self._set_log(report)
            self.status_var.set(
                f"family={product.family} arch={product.architecture} bootable={plan.spec.bootable}"
            )
        except LauncherError as exc:
            self.bootable = False
            self.plan = None
            self.launch_btn.config(state=tk.DISABLED)
            self.status_var.set("diagnosis failed")
            self._set_log(f"error: {exc}\n")
        except Exception as exc:
            self.bootable = False
            self.plan = None
            self.launch_btn.config(state=tk.DISABLED)
            self.status_var.set("diagnosis failed")
            self._set_log(f"error: {exc}\n")

    def preview_command(self) -> None:
        try:
            inspected = self._inspect()
        except Exception as exc:
            self._append_log(f"error: {exc}\n")
            return
        if inspected is None:
            return
        product, _caps, plan = inspected
        self.product = product
        self.plan = plan
        self.bootable = bool(plan.spec.bootable and plan.spec.argv)
        self.launch_btn.config(state=tk.NORMAL if self.bootable else tk.DISABLED)
        spec = plan.spec
        if not spec.argv:
            self._append_log("No executable launch plan (argv empty).\n")
            return
        text = format_command_redacted(spec.argv, windows=os.name == "nt")
        self._append_log("\nPreview (not executed):\n" + text + "\n")

    def launch(self) -> None:
        try:
            inspected = self._inspect()
        except Exception as exc:
            messagebox.showerror("Launch", str(exc))
            return
        if inspected is None:
            return
        product, _caps, plan = inspected
        self.product = product
        self.plan = plan
        self.bootable = bool(plan.spec.bootable and plan.spec.argv)
        self.launch_btn.config(state=tk.NORMAL if self.bootable else tk.DISABLED)
        if not self.bootable or not plan.spec.argv:
            messagebox.showwarning("Launch", "Launch is disabled because bootable is False.")
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Launch", "A guest process is already running.")
            return
        argv = list(plan.spec.argv)
        if self.product is not None:
            try:
                import userdata as userdata_mod

                instance = userdata_mod.prepare_instance(
                    self.product, read_only=bool(self.read_only_var.get())
                )
                if instance.userdata is not None and "-data" in argv:
                    idx = argv.index("-data")
                    argv[idx + 1] = str(instance.userdata)
            except LauncherError as exc:
                messagebox.showerror("Launch", str(exc))
                return
        self._append_log("\nStarting:\n" + format_command_redacted(argv, windows=os.name == "nt") + "\n")
        thread = threading.Thread(target=self._spawn, args=(argv,), daemon=True)
        thread.start()

    def _spawn(self, argv: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
            )
        except Exception as exc:
            self.log_queue.put(f"failed to start: {exc}\n")
            return
        self.process = proc
        self.log_queue.put(f"pid {proc.pid} started\n")
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(line)
        code = proc.wait()
        self.log_queue.put(f"process exited {code}\n")

    def stop(self) -> None:
        proc = self.process
        if proc is None or proc.poll() is not None:
            self.status_var.set("no running guest")
            return
        proc.terminate()
        self.status_var.set("sent SIGTERM")
        self.root.after(50, lambda: self._wait_then_kill(proc, 50))

    def _wait_then_kill(self, proc: subprocess.Popen, waited_ms: int) -> None:
        if proc.poll() is not None:
            self.status_var.set(f"stopped (exit {proc.returncode})")
            return
        if waited_ms >= 5000:
            proc.kill()
            self.status_var.set("sent SIGKILL after timeout")
            return
        self.root.after(200, lambda: self._wait_then_kill(proc, waited_ms + 200))

    def launch_rom_creator(self) -> None:
        try:
            from extras import rom_creator

            rom_creator.launch()
        except Exception as exc:
            messagebox.showerror("ROM Creator", f"Failed to open legacy mock: {exc}")

    def _drain_logs(self) -> None:
        try:
            while True:
                self.log.insert(tk.END, self.log_queue.get_nowait())
                self.log.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_logs)

    def _set_log(self, text: str) -> None:
        self.log.delete("1.0", tk.END)
        self.log.insert(tk.END, text)

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text)
        self.log.see(tk.END)

    def _on_close(self) -> None:
        proc = self.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    StartLoaderGUI(root)
    root.mainloop()
