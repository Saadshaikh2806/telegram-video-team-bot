"""Run locally to enter credentials; no secrets are sent anywhere except Telegram."""
from pathlib import Path
import re
import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent


def create_window():
    path = ROOT / '.env'
    window = tk.Tk()
    window.title('Video Team Bot — Setup')
    window.geometry('590x390')
    window.resizable(False, False)
    window.saved = False
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Connect your Telegram bot', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Copy the token from @BotFather, then click Paste token.\nYour token is saved only on this computer.', padding=(0, 10)).pack(anchor='w')
    token = tk.StringVar()
    admins = tk.StringVar()
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                if key.strip() == 'TELEGRAM_BOT_TOKEN':
                    token.set(value.strip().strip('\"').strip("'"))
                elif key.strip() == 'ADMIN_IDS':
                    admins.set(value.strip().strip('\"').strip("'"))
    ttk.Label(frame, text='Bot token').pack(anchor='w')
    token_row = ttk.Frame(frame)
    token_row.pack(fill='x', pady=(4, 4))
    entry = ttk.Entry(token_row, textvariable=token, show='*', width=49)
    entry.pack(side='left', fill='x', expand=True)

    def paste():
        try:
            token.set(window.clipboard_get().strip())
            entry.focus_set()
        except tk.TclError:
            messagebox.showinfo('Clipboard is empty', 'Copy the token from Telegram first.', parent=window)

    ttk.Button(token_row, text='Paste token', command=paste).pack(side='left', padx=(8, 0))
    visible = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text='Show token', variable=visible,
                    command=lambda: entry.configure(show='' if visible.get() else '*')).pack(anchor='w')
    ttk.Label(frame, text='Admin user IDs (optional for now)', padding=(0, 14, 0, 4)).pack(anchor='w')
    ttk.Entry(frame, textvariable=admins).pack(fill='x')
    ttk.Label(frame, text='Separate IDs with commas. If unknown, leave blank and send\n/whoami to your bot after starting it.', padding=(0, 7)).pack(anchor='w')

    def save():
        value = token.get().strip()
        admin_value = admins.get().strip()
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', value):
            messagebox.showerror('Check the token', 'Paste only the bot token from @BotFather, without any surrounding text.', parent=window)
            return
        if admin_value and not all(re.fullmatch(r'[0-9]+', item.strip()) for item in admin_value.split(',')):
            messagebox.showerror('Check admin IDs', 'Use numeric user IDs separated by commas, or leave this field blank.', parent=window)
            return
        try:
            source = path if path.exists() else ROOT / '.env.example'
            lines = source.read_text(encoding='utf-8-sig').splitlines()
            updates = {'TELEGRAM_BOT_TOKEN': value, 'ADMIN_IDS': admin_value}
            output = []
            for line in lines:
                key = line.split('=', 1)[0].strip() if '=' in line else ''
                if key in updates:
                    output.append(f'{key}={updates.pop(key)}')
                else:
                    output.append(line)
            output.extend(f'{key}={value}' for key, value in updates.items())
            path.write_text('\n'.join(output) + '\n', encoding='utf-8')
        except OSError:
            messagebox.showerror('Could not save', 'The settings file could not be saved. Check that this folder is writable.', parent=window)
            return
        window.saved = True
        messagebox.showinfo('Settings saved', 'Your settings are saved.\n\nIf the bot is not already starting, double-click Start Bot.cmd.\nIf it is running, restart it to apply changes.', parent=window)
        window.destroy()

    ttk.Button(frame, text='Save settings', command=save).pack(anchor='e', pady=(12, 0))
    entry.focus_set()
    return window


def main():
    window = create_window()
    window.mainloop()
    return 0 if window.saved else 1

if __name__ == '__main__':
    raise SystemExit(main())
