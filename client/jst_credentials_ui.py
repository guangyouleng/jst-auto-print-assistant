"""Credential editor: Tk stays on the UI thread; queries run in a worker."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from jst_credentials import CredentialError, FIELDS
from jst_openapi import JSTReadonlyClient, JSTQueryError


def verify_and_save(store, values, *, client_factory=JSTReadonlyClient):
    from jst_credentials import validate_credentials
    values = validate_credentials(values)
    client_factory(store.legacy_path, credentials=values).check()
    return store.save(values)


class CredentialDialog:
    def __init__(self, root, store, on_saved, reason=''):
        self.store = store
        self.on_saved = on_saved
        self.busy = False
        self.results = queue.Queue()
        self.window = tk.Toplevel(root)
        self.window.title('聚水潭凭据设置')
        self.window.transient(root)
        self.window.resizable(False, False)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=reason or '填写聚水潭开放 API 凭据，验证后使用 Windows DPAPI 加密保存。',
                  wraplength=470).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 12))
        try:
            values = store.load()
        except CredentialError:
            # Never silently fall back to a plaintext file during business use.
            values = store.legacy_values() if not store.path.exists() else {}
        self.variables = {}
        self.entries = []
        for row, field in enumerate(FIELDS, start=1):
            ttk.Label(frame, text=field).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=6)
            variable = tk.StringVar(value=values.get(field, ''))
            self.variables[field] = variable
            entry = ttk.Entry(frame, textvariable=variable, width=46,
                              show='' if field == 'app_key' else '*')
            entry.grid(row=row, column=1, sticky='ew', pady=6)
            self.entries.append(entry)
        self.status = tk.StringVar(value='仅验证只读查询；保存后不会自动启动打单。')
        ttk.Label(frame, textvariable=self.status, wraplength=470).grid(
            row=4, column=0, columnspan=2, sticky='w', pady=12)
        self.save_button = ttk.Button(frame, text='验证并保存', command=self.save)
        self.save_button.grid(row=5, column=1, sticky='e')
        self.window.grab_set()
        self.entries[0].focus_set()

    def close(self):
        if self.busy:
            return
        for variable in self.variables.values():
            variable.set('')
        self.window.destroy()

    def save(self):
        if self.busy:
            return
        values = {field: variable.get() for field, variable in self.variables.items()}
        self.busy = True
        self.save_button.configure(state='disabled')
        for entry in self.entries:
            entry.configure(state='disabled')
        self.status.set('正在验证聚水潭只读查询并加密保存，请稍候…')

        def work():
            try:
                warning = verify_and_save(self.store, values)
            except (CredentialError, JSTQueryError) as exc:
                self.results.put((False, str(exc)))
            except Exception:
                self.results.put((False, '凭据验证或保存失败；请检查网络及本机权限，原配置未替换'))
            else:
                self.results.put((True, warning))
            finally:
                values.clear()

        threading.Thread(target=work, daemon=True).start()
        self.window.after(100, self.poll)

    def poll(self):
        try:
            success, message = self.results.get_nowait()
        except queue.Empty:
            self.window.after(100, self.poll)
            return
        self.busy = False
        if success:
            self.on_saved(message)
            self.close()
        else:
            self.status.set(message)
            self.save_button.configure(state='normal')
            for entry in self.entries:
                entry.configure(state='normal')
