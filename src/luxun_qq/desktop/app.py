"""Tk desktop UI. Widgets are touched only by the main thread."""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .controller import DesktopController
from .bundled_qq import inspect_bundle, launch_bundle, login_url, prepare_bundle
from .runtime import import_components, launch_qq
from .settings import Settings, SettingsStore, app_directory, resource_directory


class DesktopApp:
    def __init__(self, root: tk.Tk, directory: Path):
        self.root = root
        self.directory = directory
        self.store = SettingsStore(directory)
        self.controller = DesktopController(directory)
        self.ui_events: queue.Queue = queue.Queue()
        self.items: dict[str, dict] = {}
        self.current_revision = ""
        self.state = "stopped"
        self.busy = False
        self.closing = False
        self.importing = False
        self.qq_process = None
        self.bundle_source = resource_directory() / "qq-bundle"
        self.widgets_to_lock = []
        self.settings_error = ""
        try:
            settings = self.store.load()
        except Exception as exc:
            settings = Settings()
            self.settings_error = f"无法读取保存的配置：{exc}。原文件未修改，请重新填写后保存。"
        self.vars = {}
        for field in fields(Settings):
            value = getattr(settings, field.name)
            variable = tk.BooleanVar if isinstance(value, bool) else tk.StringVar
            self.vars[field.name] = variable(root, value=value)
        root.title("Luxun QQ · AI 回复助手")
        root.geometry("1140x850")
        root.minsize(1000, 760)
        root.configure(bg="#f4f6fa")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabelframe", background="#f4f6fa", bordercolor="#d8dfec")
        style.configure("TCheckbutton", background="#f4f6fa")
        style.configure("TRadiobutton", background="#f4f6fa")
        style.configure("TNotebook", background="#f4f6fa", bordercolor="#d8dfec")
        style.configure("TNotebook.Tab", padding=(10, 6))
        style.configure("TLabel", background="#f4f6fa", foreground="#202c42", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 10))
        style.configure("Accent.TButton", background="#335de5", foreground="white")
        style.map("Accent.TButton", background=[("active", "#254cc7")])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Muted.TLabel", foreground="#62728b")
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=36, font=("Microsoft YaHei UI", 10))
        self._build()
        self._refresh_controls()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(80, self._poll)
        if self.settings_error:
            self._log(self.settings_error)
            self.status.set("配置加载失败，请查看运行日志并重新填写。")

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        header = ttk.Frame(self.root, padding=(24, 18))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Luxun QQ", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="AI 回复助手  /  全部代码由 AI 开发", style="Muted.TLabel").pack(side="left", padx=20)
        controls = ttk.Frame(self.root, padding=(24, 0, 24, 14))
        controls.grid(row=1, column=0, sticky="ew")
        self.start_button = ttk.Button(controls, text="启动回复服务", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.demo_button = ttk.Button(controls, text="离线演示", command=lambda: self.start(demo=True))
        self.demo_button.pack(side="left", padx=8)
        self.pause_button = ttk.Button(controls, text="暂停", command=self.pause)
        self.pause_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.auto_button = ttk.Checkbutton(controls, text="自动托管（发送第一条候选）",
                                           variable=self.vars["automatic"], command=self.toggle_auto)
        self.auto_button.pack(side="right")
        self.status = tk.StringVar(value="尚未启动 · 可先使用离线演示，无需 QQ 或模型")
        ttk.Label(self.root, textvariable=self.status, padding=(24, 0, 24, 12),
                  style="Muted.TLabel").grid(row=2, column=0, sticky="ew")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.grid(row=3, column=0, sticky="nsew", padx=24, pady=(0, 14))
        self.work = ttk.Frame(self.tabs, padding=14)
        self.config_tab = ttk.Frame(self.tabs, padding=14)
        self.logs_tab = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(self.work, text="  回复工作台  ")
        self.tabs.add(self.config_tab, text="  连接与模型  ")
        self.tabs.add(self.logs_tab, text="  运行日志  ")
        self._workbench()
        self._configuration()
        self.log_text = tk.Text(self.logs_tab, wrap="word", font=("Consolas", 10),
                                bg="#172236", fg="#dce5f5", padx=14, pady=12, state="disabled")
        self.log_text.pack(fill="both", expand=True)
        self.footer = tk.StringVar(value="控制和候选只在本窗口显示；QQ 仅发送最终回复。")
        ttk.Label(self.root, textvariable=self.footer, style="Muted.TLabel",
                  padding=(24, 0, 24, 12)).grid(row=4, column=0, sticky="ew")

    def _workbench(self):
        demo = ttk.LabelFrame(self.work, text="离线演示消息", padding=8)
        demo.pack(fill="x", pady=(0, 10))
        self.demo_peer = tk.StringVar(value="10001")
        self.demo_text = tk.StringVar(value="今天上班好累啊，被领导骂了")
        ttk.Entry(demo, textvariable=self.demo_peer, width=12).pack(side="left", padx=(0, 8))
        ttk.Entry(demo, textvariable=self.demo_text).pack(side="left", fill="x", expand=True)
        self.simulate_button = ttk.Button(demo, text="模拟收到消息", command=self.simulate)
        self.simulate_button.pack(side="right", padx=(8, 0))
        body = ttk.Panedwindow(self.work, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        right = ttk.Frame(body, padding=(14, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=4)
        ttk.Label(left, text="待回复会话").pack(anchor="w", pady=(0, 8))
        self.conversations = ttk.Treeview(left, columns=("message",), show="tree headings", selectmode="browse", height=12)
        self.conversations.heading("#0", text="QQ 号")
        self.conversations.heading("message", text="最新消息")
        self.conversations.column("#0", width=85, stretch=False)
        self.conversations.column("message", width=155)
        self.conversations.pack(fill="both", expand=True)
        self.conversations.bind("<<TreeviewSelect>>", lambda _: self._select())
        self.incoming = tk.StringVar(value="等待消息…\n启动服务后，候选回复会显示在这里。")
        ttk.Label(right, textvariable=self.incoming, wraplength=620, padding=(0, 4, 0, 10)).pack(fill="x")
        self.candidate_boxes = []
        self.send_buttons = []
        for index in range(3):
            card = ttk.LabelFrame(right, text=f"候选 {index + 1} · 可直接编辑", padding=8)
            card.pack(fill="both", expand=True, pady=(0, 8))
            editor = tk.Text(card, height=3, wrap="word", font=("Microsoft YaHei UI", 10),
                             relief="flat", bg="white", padx=8, pady=6, undo=True)
            editor.pack(side="left", fill="both", expand=True)
            button = ttk.Button(card, text="发送这条", command=lambda i=index: self.reply("send", i))
            button.pack(side="right", padx=(8, 0))
            self.candidate_boxes.append(editor)
            self.send_buttons.append(button)
        actions = ttk.Frame(right)
        actions.pack(fill="x")
        self.regenerate_button = ttk.Button(actions, text="重新生成", command=lambda: self.reply("regenerate"))
        self.regenerate_button.pack(side="left")
        self.discard_button = ttk.Button(actions, text="丢弃", command=lambda: self.reply("discard"))
        self.discard_button.pack(side="left", padx=8)
        self.analysis_button = ttk.Button(actions, text="查看分析", command=self.analysis)
        self.analysis_button.pack(side="right")

    def _entry(self, parent, row, label, key, *, choices=None, secret=False, browse=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        if choices:
            widget = ttk.Combobox(parent, textvariable=self.vars[key], values=choices, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=self.vars[key], show="•" if secret else "")
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)
        self.widgets_to_lock.append((widget, "readonly" if choices else "normal"))
        if browse:
            button = ttk.Button(parent, text="选择…", command=lambda: self.browse(key))
            button.grid(row=row, column=2, padx=(6, 0))
            self.widgets_to_lock.append((button, "normal"))
        return widget

    def _check(self, parent, row, text, key):
        widget = ttk.Checkbutton(parent, text=text, variable=self.vars[key])
        widget.grid(row=row, column=0, columnspan=3, sticky="w", pady=5)
        self.widgets_to_lock.append((widget, "normal"))

    def _configuration(self):
        sections = ttk.Notebook(self.config_tab)
        sections.pack(fill="both", expand=True)
        model_tab = ttk.Frame(sections, padding=16)
        qq_tab = ttk.Frame(sections, padding=16)
        components = ttk.Frame(sections, padding=16)
        sections.add(model_tab, text="模型生成")
        sections.add(qq_tab, text="QQ 与回复规则")
        sections.add(components, text="运行组件")
        modes = ttk.Frame(model_tab)
        modes.pack(fill="x", pady=(0, 12))
        for value, label in (("api", "大模型 API"), ("local_service", "本地模型服务"), ("local_file", "本地 GGUF 文件")):
            button = ttk.Radiobutton(modes, text=label, value=value, variable=self.vars["mode"], command=self._model_panel)
            button.pack(side="left", padx=(0, 16))
            self.widgets_to_lock.append((button, "normal"))
        self.model_panels = {}
        for mode in ("api", "local_service", "local_file"):
            self.model_panels[mode] = ttk.Frame(model_tab)
        api = self.model_panels["api"]
        self._entry(api, 0, "API 地址", "api_url")
        self._entry(api, 1, "模型名称", "api_model")
        self._entry(api, 2, "API Key", "api_key", secret=True)
        ttk.Label(api, text="支持 OpenAI 兼容接口。密钥使用 Windows 当前用户加密后保存。",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=12)
        local = self.model_panels["local_service"]
        self._entry(local, 0, "服务协议", "local_protocol", choices=("ollama", "openai"))
        self._entry(local, 1, "服务地址", "local_url")
        self._entry(local, 2, "模型名称", "local_model")
        ttk.Label(local, text="连接已运行的 Ollama、LM Studio 或 llama.cpp 服务。", style="Muted.TLabel").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=12)
        file_panel = self.model_panels["local_file"]
        self._entry(file_panel, 0, "推理引擎", "llama_exe", browse=True)
        self._entry(file_panel, 1, "GGUF 模型", "gguf_path", browse=True)
        self._entry(file_panel, 2, "GPU 层数（0 为 CPU）", "gpu_layers")
        self._entry(file_panel, 3, "上下文长度", "context_size")
        ttk.Label(file_panel, text="启动服务时自动加载模型，停止时释放；分片模型需保留所有分片。",
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=12)
        self._model_panel()
        self._entry(qq_tab, 0, "QQ WebSocket 地址", "ws_url")
        self._entry(qq_tab, 1, "连接 Token", "ws_token", secret=True)
        self._entry(qq_tab, 2, "回复白名单（QQ 号）", "whitelist")
        self._check(qq_tab, 3, "回复所有私聊（关闭时只回复白名单）", "target_all")
        self._entry(qq_tab, 4, "回复角色", "persona", choices=("emotion_master_male", "luxun"))
        self._entry(qq_tab, 5, "最短发送延迟（秒）", "delay_min")
        self._entry(qq_tab, 6, "最长发送延迟（秒）", "delay_max")
        self._check(qq_tab, 7, "保存聊天记忆，生成时参考相关历史", "memory")
        self._check(qq_tab, 8, "两阶段生成（先写回复，再整理分析）", "two_stage")
        ttk.Label(qq_tab, text="情感大师：emotion_master_male    鲁迅式锐评：luxun\n"
                  "仅支持私聊；登录账号从 QQ 连接自动识别。设置在下次启动时生效。",
                  style="Muted.TLabel").grid(row=9, column=0, columnspan=3, sticky="w", pady=10)
        self._entry(components, 0, "QQ/NapCat 启动程序", "qq_launcher", browse=True)
        launch = ttk.Button(components, text="启动已配置的 QQ 组件", command=self.launch_component)
        launch.grid(row=1, column=1, sticky="w", pady=8)
        self.widgets_to_lock.append((launch, "normal"))
        self.qq_bundle_button = ttk.Button(components, text="启动内置 QQ/NapCat", style="Accent.TButton",
                                           command=self.launch_bundled)
        self.qq_bundle_button.grid(row=1, column=0, sticky="w", pady=8)
        self.widgets_to_lock.append((self.qq_bundle_button, "normal"))
        self.qq_login_button = ttk.Button(components, text="打开 QQ 登录页", command=self.open_qq_login)
        self.qq_login_button.grid(row=1, column=2, sticky="w", pady=8)
        ttk.Label(components, text="内置版无需事先安装 QQ/NapCat，点击启动后在登录页完成扫码。\n"
                  "QQ/NapCat 将释放到用户数据目录；不包含推理引擎和模型权重。", style="Muted.TLabel").grid(
                      row=2, column=0, columnspan=3, sticky="w", pady=10)
        self.import_button = ttk.Button(components, text="导入组件目录…", command=self.import_folder)
        self.import_button.grid(row=3, column=0, sticky="w", pady=8)
        self.bundled_button = ttk.Button(components, text="仅释放内置组件（不启动）", command=self.import_bundled)
        self.bundled_button.grid(row=3, column=1, sticky="w", pady=8)
        self.widgets_to_lock.extend([(self.import_button, "normal"), (self.bundled_button, "normal")])
        ttk.Label(components, text="组件目录可包含 llama/、models/、qq/；也可只包含其中一部分。\n"
                  "导入后自动填入已识别路径。多模型或多个启动器请手动选择。\n"
                  "API 模式不需要模型文件和本地推理引擎。", style="Muted.TLabel").grid(
                      row=4, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Label(components, text=f"配置、记忆及日志保存在：\n{self.directory}", wraplength=800,
                  style="Muted.TLabel").grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        self.save_button = ttk.Button(self.config_tab, text="保存配置", style="Accent.TButton", command=self.save)
        self.save_button.pack(anchor="e", pady=(12, 0))

    def _model_panel(self):
        for panel in self.model_panels.values():
            panel.pack_forget()
        self.model_panels.get(self.vars["mode"].get(), self.model_panels["api"]).pack(fill="both", expand=True)

    def settings(self) -> Settings:
        values = {key: var.get() for key, var in self.vars.items()}
        for key in ("gpu_layers", "context_size"):
            values[key] = int(values[key])
        for key in ("delay_min", "delay_max"):
            values[key] = float(values[key])
        return Settings(**values)

    def _error(self, error):
        self._log(str(error))
        self.status.set(str(error))
        messagebox.showerror("Luxun QQ", str(error), parent=self.root)

    def save(self):
        try:
            settings = self.settings()
            settings.validate(demo=True)
            self.store.save(settings)
            self.status.set("配置已保存；运行中的服务使用启动时的配置。")
        except Exception as exc:
            self._error(exc)

    def start(self, demo=False):
        try:
            self.controller.start(self.settings(), demo=demo)
            self.footer.set("离线演示：所有发送均为模拟，不会连接 QQ。" if demo
                            else "控制和候选只在本窗口显示；QQ 仅发送最终回复。")
            self.state = "starting"
            self.tabs.select(self.work)
            self._refresh_controls()
        except Exception as exc:
            self._error(exc)

    def pause(self):
        self.controller.set_paused(not self.controller.paused.is_set())

    def stop(self):
        self.state = "stopping"
        self.controller.stop()
        self._refresh_controls()

    def toggle_auto(self):
        if self.controller.running and not self.controller.cancelled.is_set():
            self._submit("automatic", enabled=self.vars["automatic"].get())

    def _submit(self, kind, **payload):
        try:
            self.controller.submit(kind, **payload)
        except Exception as exc:
            self._error(exc)

    def simulate(self):
        self._submit("simulate", peer=self.demo_peer.get(), text=self.demo_text.get())

    def selected(self):
        selection = self.conversations.selection()
        return self.items.get(selection[0]) if selection else None

    def reply(self, kind, index=0):
        item = self.selected()
        if item is None:
            return
        payload = {"peer": item["peer"], "revision": item["revision"]}
        if kind == "send":
            payload["text"] = self.candidate_boxes[index].get("1.0", "end-1c")
        self._submit(kind, **payload)

    def _select(self):
        item = self.selected()
        revision = item["revision"] if item else ""
        if revision == self.current_revision:
            self._refresh_controls()
            return
        self.current_revision = revision
        self.incoming.set(f"{item['peer']} 发来：\n{item['incoming'][:500]}" if item else "等待消息…")
        for i, editor in enumerate(self.candidate_boxes):
            editor.configure(state="normal")
            editor.delete("1.0", "end")
            if item and i < len(item["candidates"]):
                editor.insert("1.0", item["candidates"][i])
        self._refresh_controls()

    def analysis(self):
        item = self.selected()
        if item:
            popup = tk.Toplevel(self.root)
            popup.title("候选分析")
            popup.geometry("800x600")
            body = tk.Text(popup, wrap="word", font=("Microsoft YaHei UI", 11), padx=16, pady=16)
            body.pack(fill="both", expand=True)
            body.insert("1.0", item["analysis"])
            body.configure(state="disabled")

    def browse(self, key):
        types = [("GGUF 模型", "*.gguf")] if key == "gguf_path" else [("启动程序", "*.exe *.bat *.cmd")]
        path = filedialog.askopenfilename(parent=self.root, filetypes=types + [("所有文件", "*.*")])
        if path:
            self.vars[key].set(path)

    def launch_component(self):
        try:
            launch_qq(self.vars["qq_launcher"].get())
            self.status.set("已请求启动 QQ 组件，请完成登录后启动回复服务。")
        except Exception as exc:
            self._error(exc)

    def import_folder(self):
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            self._import(Path(folder))

    def import_bundled(self):
        if (self.bundle_source / "manifest.json").exists():
            self._prepare_qq(launch=False)
        else:
            self._import(resource_directory() / "components")

    def launch_bundled(self):
        if self.qq_process is not None and self.qq_process.poll() is None:
            self.status.set("内置 QQ/NapCat 已在运行，请打开登录页完成扫码。")
            return
        self._prepare_qq(launch=True)

    def _prepare_qq(self, *, launch):
        if self.importing:
            return
        if not (self.bundle_source / "manifest.json").is_file():
            self._error("此 EXE 未包含 QQ/NapCat，请使用内置组件版，或指定外部启动程序。")
            return
        self.importing = True
        self.status.set("正在准备内置 QQ/NapCat，首次释放可能需要几分钟…")
        self._refresh_controls()
        def work():
            try:
                result = prepare_bundle(self.bundle_source, self.directory / "components",
                                        lambda text: self.ui_events.put({"kind": "bundle_progress", "message": text}))
                process = launch_bundle(Path(result["qq_bundle_dir"])) if launch else None
                self.ui_events.put({"kind": "qq_ready", "result": result, "process": process})
            except Exception as exc:
                self.ui_events.put({"kind": "import_failed", "message": str(exc)})
        threading.Thread(target=work, daemon=True, name="qq-bundle-prepare").start()

    def open_qq_login(self):
        try:
            import webbrowser
            directory = self.vars["qq_bundle_dir"].get()
            if not directory:
                raise ValueError("请先点击“启动内置 QQ/NapCat”。")
            webbrowser.open(login_url(Path(directory)))
            self.status.set("已打开 QQ 登录页；若尚未就绪，请等待几秒后刷新。")
        except Exception as exc:
            self._error(exc)

    def _import(self, source):
        if self.importing:
            return
        self.importing = True
        self.status.set("正在复制组件（大模型可能需要几分钟）…")
        self._refresh_controls()
        def work():
            try:
                result = import_components(source, self.directory / "components")
                self.ui_events.put({"kind": "imported", "result": result})
            except Exception as exc:
                self.ui_events.put({"kind": "import_failed", "message": str(exc)})
        threading.Thread(target=work, daemon=True).start()

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S") + "  " + message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2500:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _event(self, event):
        kind = event["kind"]
        if kind == "status":
            self.state = event["state"]
            self.status.set(event["message"])
            self._log(event["message"])
        elif kind == "busy":
            self.busy = event["value"]
        elif kind in ("error", "notice", "log"):
            self._log(event["message"])
            if kind != "log":
                self.status.set(event["message"].splitlines()[0][:130])
        elif kind == "sent":
            label = "模拟发送" if self.controller.demo else "已发送"
            self._log(f"[{label}] → {event['peer']}：{event['text']}")
            self.status.set(f"{label}给 {event['peer']}")
        elif kind == "pending":
            old_selection = self.conversations.selection()
            self.items = {item["peer"]: item for item in event["items"]}
            for peer in self.conversations.get_children():
                if peer not in self.items:
                    self.conversations.delete(peer)
            for peer, item in self.items.items():
                if self.conversations.exists(peer):
                    self.conversations.item(peer, text=peer, values=(item["incoming"][:35],))
                else:
                    self.conversations.insert("", "end", iid=peer, text=peer, values=(item["incoming"][:35],))
            selected = old_selection[0] if old_selection and old_selection[0] in self.items else next(iter(self.items), None)
            if selected:
                self.conversations.selection_set(selected)
            self._select()
        elif kind in ("imported", "import_failed"):
            self.importing = False
            if kind == "imported":
                for key, value in event["result"].items():
                    if key in self.vars:
                        self.vars[key].set(value)
                self.status.set("组件已导入，请检查路径并保存配置。")
                self._log("组件导入到：" + event["result"]["directory"])
            else:
                self._error(event["message"])
        elif kind == "bundle_progress":
            self.status.set(event["message"])
        elif kind == "qq_ready":
            self.importing = False
            for key in ("qq_bundle_dir", "ws_url", "ws_token"):
                self.vars[key].set(event["result"][key])
            self.qq_process = event["process"]
            try:
                self.store.save(self.settings())
            except Exception as exc:
                self._log(f"连接设置尚未保存：{exc}")
            self.status.set("内置 QQ/NapCat 已请求启动，请打开 QQ 登录页扫码，再启动回复服务。"
                            if self.qq_process else "内置组件已释放，尚未启动 QQ/NapCat。")
            self._log(self.status.get())
        self._refresh_controls()

    def _refresh_controls(self):
        idle = self.state == "stopped" and not self.controller.running and not self.importing
        active = self.state in ("running", "paused") and not self.controller.cancelled.is_set()
        can_reply = active and not self.controller.paused.is_set() and not self.busy and bool(self.selected())
        for button in (self.start_button, self.demo_button):
            button.configure(state="normal" if idle else "disabled")
        self.pause_button.configure(state="normal" if active else "disabled",
                                    text="恢复" if self.controller.paused.is_set() else "暂停")
        self.stop_button.configure(state="normal" if self.state in ("starting", "running", "paused") else "disabled")
        self.auto_button.configure(state="normal" if idle or (active and not self.busy) else "disabled")
        self.simulate_button.configure(state="normal" if active and self.controller.demo and not self.busy
                                       and not self.controller.paused.is_set() else "disabled")
        for button in self.send_buttons + [self.regenerate_button, self.discard_button]:
            button.configure(state="normal" if can_reply else "disabled")
        self.analysis_button.configure(state="normal" if self.selected() else "disabled")
        for editor in self.candidate_boxes:
            editor.configure(state="normal" if can_reply else "disabled")
        for widget, normal in self.widgets_to_lock:
            widget.configure(state=normal if idle else "disabled")
        self.save_button.configure(state="normal" if idle else "disabled")
        self.qq_bundle_button.configure(state="normal" if idle and (self.bundle_source / "manifest.json").is_file() else "disabled")
        self.qq_login_button.configure(state="normal" if self.vars["qq_bundle_dir"].get() and not self.importing else "disabled")

    def _poll(self):
        for source in (self.controller.events, self.ui_events):
            for _ in range(100):
                try:
                    event = source.get_nowait()
                except queue.Empty:
                    break
                self._event(event)
        self._refresh_controls()
        if self.closing and not self.controller.running:
            self.root.destroy()
            return
        self.root.after(80, self._poll)

    def close(self):
        if self.importing:
            messagebox.showinfo("正在导入", "请等待组件复制完成后再关闭。", parent=self.root)
            return
        if self.controller.running:
            self.closing = True
            self.stop()
            self.status.set("正在关闭：已禁止新发送，等待在途请求结束并释放模型…")
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Luxun QQ desktop")
    parser.add_argument("--smoke-test", metavar="REPORT", help="build UI and run an offline roundtrip; save JSON report")
    parser.add_argument("--inspect-bundle", metavar="REPORT", help="verify embedded QQ/NapCat without extracting or launching it")
    args = parser.parse_args()
    if args.inspect_bundle:
        report = inspect_bundle(resource_directory() / "qq-bundle", verify=True)
        Path(args.inspect_bundle).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    if args.smoke_test:
        import tempfile
        directory = Path(tempfile.mkdtemp(prefix="luxun-desktop-smoke-"))
        root.withdraw()
    else:
        directory = app_directory()
    app = DesktopApp(root, directory)
    if args.smoke_test:
        started = time.monotonic()
        stage = {"value": "start", "ok": False}
        app.start(demo=True)
        def check():
            if stage["value"] == "start" and app.state == "running":
                app.simulate()
                stage["value"] = "generate"
            elif stage["value"] == "generate" and app.items and not app.busy:
                item = app.selected()
                stage["ok"] = bool(item and len(item["candidates"]) == 3)
                app.reply("send", 0)
                stage["value"] = "send"
            elif stage["value"] == "send" and not app.items and not app.busy:
                app.stop()
                stage["value"] = "stop"
            elif stage["value"] == "stop" and not app.controller.running:
                report = {"ok": stage["ok"], "mode": "offline", "qq_connected": False,
                          "widgets": len(app.root.winfo_children()), "data_directory": str(directory)}
                Path(args.smoke_test).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
                root.destroy()
                return
            if time.monotonic() - started > 25:
                Path(args.smoke_test).write_text(json.dumps({"ok": False, "stage": stage["value"]}), encoding="utf-8")
                app.controller.stop()
                root.destroy()
                return
            root.after(100, check)
        root.after(100, check)
    root.mainloop()


if __name__ == "__main__":
    main()
