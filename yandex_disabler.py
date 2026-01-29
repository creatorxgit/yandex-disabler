import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging
import subprocess
import sys
import os
import ctypes
import winreg
from typing import Optional, Callable, List, Dict, Tuple
from dataclasses import dataclass, field
from contextlib import contextmanager
import json
from pathlib import Path

# Константы
__version__ = "1.0"
APP_NAME = "YandexBrowserBlocker"
CONFIG_FILE = Path(os.getenv('APPDATA', '.')) / APP_NAME / "config.json"

# Пути для блокировки в реестре IFEO
IFEO_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options"

# Исполняемые файлы Яндекс Браузера (все возможные варианты)
BLOCKED_EXECUTABLES = [
    "browser.exe",
    "yandex.exe",
    "YandexBrowser.exe",
    "yandexbrowser.exe",
    "ya.exe",
]

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    """Конфигурация приложения"""
    show_notifications: bool = True
    minimize_to_tray: bool = True
    blocked_executables: List[str] = field(default_factory=lambda: BLOCKED_EXECUTABLES.copy())
    
    def save(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'show_notifications': self.show_notifications,
                    'minimize_to_tray': self.minimize_to_tray,
                    'blocked_executables': self.blocked_executables,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения конфигурации: {e}")
    
    @classmethod
    def load(cls) -> 'AppConfig':
        config = cls()
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                config.show_notifications = data.get('show_notifications', True)
                config.minimize_to_tray = data.get('minimize_to_tray', True)
                config.blocked_executables = data.get('blocked_executables', BLOCKED_EXECUTABLES.copy())
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
        return config


@contextmanager
def temp_tk_root():
    """Контекстный менеджер для временного окна Tk"""
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def is_admin() -> bool:
    """Проверяет права администратора"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_as_admin() -> bool:
    """Перезапускает с правами администратора"""
    try:
        script = os.path.abspath(sys.argv[0])
        params = ' '.join([f'"{arg}"' for arg in sys.argv[1:]])
        
        if script.endswith('.py'):
            executable = sys.executable
            params = f'"{script}" {params}'
        else:
            executable = script
        
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, 1
        )
        return ret > 32
    except Exception as e:
        logger.error(f"Ошибка при запросе прав: {e}")
        return False


class RegistryBlocker:
    """
    Блокировка через реестр Windows (Image File Execution Options).
    
    Это МОМЕНТАЛЬНАЯ блокировка - Windows не даст запустить процесс вообще.
    Работает на уровне ядра системы.
    """
    
    # Команда-заглушка, которая ничего не делает
    BLOCKER_CMD = "nul"
    
    @classmethod
    def _get_registry_access(cls, write: bool = False) -> int:
        """Получает флаги доступа к реестру"""
        access = winreg.KEY_READ if not write else winreg.KEY_ALL_ACCESS
        # Для 64-битных систем нужен дополнительный флаг
        if sys.maxsize > 2**32:
            access |= winreg.KEY_WOW64_64KEY
        return access
    
    @classmethod
    def _validate_exe_name(cls, exe_name: str) -> bool:
        """Валидация имени исполняемого файла"""
        import re
        return bool(re.match(r'^[\w\-\.]+\.exe$', exe_name, re.IGNORECASE))
    
    @classmethod
    def block_executable(cls, exe_name: str) -> Tuple[bool, str]:
        """
        Блокирует запуск указанного исполняемого файла.
        
        Создаёт ключ в IFEO с Debugger=nul, что предотвращает запуск.
        """
        if not cls._validate_exe_name(exe_name):
            return False, f"Недопустимое имя: {exe_name}"
        
        key_path = f"{IFEO_PATH}\\{exe_name}"
        
        try:
            # Создаём или открываем ключ
            key = winreg.CreateKeyEx(
                winreg.HKEY_LOCAL_MACHINE,
                key_path,
                0,
                cls._get_registry_access(write=True)
            )
            
            try:
                # Устанавливаем Debugger на nul - это блокирует запуск
                winreg.SetValueEx(key, "Debugger", 0, winreg.REG_SZ, cls.BLOCKER_CMD)
                logger.info(f"✅ Заблокирован: {exe_name}")
                return True, f"Заблокирован: {exe_name}"
            finally:
                winreg.CloseKey(key)
                
        except PermissionError:
            msg = f"Нет прав администратора для блокировки: {exe_name}"
            logger.error(f"❌ {msg}")
            return False, msg
        except Exception as e:
            msg = f"Ошибка блокировки {exe_name}: {e}"
            logger.error(f"❌ {msg}")
            return False, msg
    
    @classmethod
    def unblock_executable(cls, exe_name: str) -> Tuple[bool, str]:
        """
        Разблокирует запуск указанного исполняемого файла.
        
        Удаляет ключ Debugger из IFEO.
        """
        if not cls._validate_exe_name(exe_name):
            return False, f"Недопустимое имя: {exe_name}"
        
        key_path = f"{IFEO_PATH}\\{exe_name}"
        
        try:
            # Пробуем открыть и удалить значение Debugger
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    key_path,
                    0,
                    cls._get_registry_access(write=True)
                )
                try:
                    winreg.DeleteValue(key, "Debugger")
                except FileNotFoundError:
                    pass  # Значения уже нет
                finally:
                    winreg.CloseKey(key)
            except FileNotFoundError:
                pass  # Ключа уже нет
            
            # Пробуем удалить сам ключ (если он пустой)
            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key_path)
            except (FileNotFoundError, OSError):
                pass  # Ключа нет или он не пустой
            
            logger.info(f"✅ Разблокирован: {exe_name}")
            return True, f"Разблокирован: {exe_name}"
            
        except PermissionError:
            msg = f"Нет прав администратора для разблокировки: {exe_name}"
            logger.error(f"❌ {msg}")
            return False, msg
        except Exception as e:
            msg = f"Ошибка разблокировки {exe_name}: {e}"
            logger.error(f"❌ {msg}")
            return False, msg
    
    @classmethod
    def is_blocked(cls, exe_name: str) -> bool:
        """Проверяет, заблокирован ли исполняемый файл"""
        if not cls._validate_exe_name(exe_name):
            return False
        
        key_path = f"{IFEO_PATH}\\{exe_name}"
        
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                key_path,
                0,
                cls._get_registry_access(write=False)
            )
            try:
                value, _ = winreg.QueryValueEx(key, "Debugger")
                return value == cls.BLOCKER_CMD
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.debug(f"Ошибка проверки {exe_name}: {e}")
            return False
    
    @classmethod
    def block_all(cls, executables: List[str]) -> Tuple[bool, List[str]]:
        """Блокирует все указанные исполняемые файлы"""
        messages = []
        all_success = True
        
        for exe in executables:
            success, msg = cls.block_executable(exe)
            messages.append(msg)
            if not success:
                all_success = False
        
        return all_success, messages
    
    @classmethod
    def unblock_all(cls, executables: List[str]) -> Tuple[bool, List[str]]:
        """Разблокирует все указанные исполняемые файлы"""
        messages = []
        all_success = True
        
        for exe in executables:
            success, msg = cls.unblock_executable(exe)
            messages.append(msg)
            if not success:
                all_success = False
        
        return all_success, messages
    
    @classmethod
    def get_status(cls, executables: List[str]) -> Dict[str, bool]:
        """Возвращает статус блокировки для всех исполняемых файлов"""
        return {exe: cls.is_blocked(exe) for exe in executables}
    
    @classmethod
    def is_any_blocked(cls, executables: List[str]) -> bool:
        """Проверяет, заблокирован ли хотя бы один файл"""
        return any(cls.is_blocked(exe) for exe in executables)
    
    @classmethod
    def get_blocked_count(cls, executables: List[str]) -> int:
        """Возвращает количество заблокированных файлов"""
        return sum(1 for exe in executables if cls.is_blocked(exe))


class ProcessKiller:
    """Убивает уже запущенные процессы Яндекс Браузера"""
    
    YANDEX_INDICATORS = ("yandex", "yabrowser", "yandexbrowser")
    
    @classmethod
    def kill_all_yandex(cls) -> List[str]:
        """Завершает все процессы Яндекс Браузера"""
        killed = []
        
        try:
            import psutil
        except ImportError:
            # Если psutil недоступен, используем taskkill
            return cls._kill_with_taskkill()
        
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                proc_info = proc.info
                if cls._is_yandex_browser(proc_info):
                    proc.kill()
                    name = proc_info.get('name', 'Unknown')
                    killed.append(name)
                    logger.info(f"Завершён процесс: {name} (PID: {proc_info.get('pid')})")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
            except Exception as e:
                logger.debug(f"Ошибка при завершении процесса: {e}")
        
        return killed
    
    @classmethod
    def _is_yandex_browser(cls, proc_info: dict) -> bool:
        """Проверяет, является ли процесс Яндекс Браузером"""
        name = (proc_info.get('name') or '').lower()
        exe = (proc_info.get('exe') or '').lower()
        cmdline = proc_info.get('cmdline') or []
        
        # Проверяем путь к исполняемому файлу
        for indicator in cls.YANDEX_INDICATORS:
            if indicator in exe:
                return True
        
        # Для browser.exe проверяем командную строку
        if name == 'browser.exe':
            cmdline_str = ' '.join(cmdline).lower()
            for indicator in cls.YANDEX_INDICATORS:
                if indicator in cmdline_str:
                    return True
        
        return False
    
    @classmethod
    def _kill_with_taskkill(cls) -> List[str]:
        """Fallback: завершение через taskkill"""
        killed = []
        
        for exe_name in BLOCKED_EXECUTABLES:
            try:
                result = subprocess.run(
                    ['taskkill', '/F', '/IM', exe_name],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    killed.append(exe_name)
                    logger.info(f"Завершён через taskkill: {exe_name}")
            except Exception as e:
                logger.debug(f"Ошибка taskkill для {exe_name}: {e}")
        
        return killed


class YandexBlockerApp:
    """Главное приложение блокировщика"""
    
    def __init__(self):
        self.config = AppConfig.load()
        self.root = tk.Tk()
        self._setup_window()
        self._create_widgets()
        self._update_status()
    
    def _setup_window(self):
        """Настройка главного окна"""
        self.root.title("🛡️ Блокировщик Яндекс Браузера")
        self.root.geometry("500x550")
        self.root.resizable(False, False)
        
        # Центрирование окна
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 500) // 2
        y = (self.root.winfo_screenheight() - 550) // 2
        self.root.geometry(f"+{x}+{y}")
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
    
    def _create_widgets(self):
        """Создание виджетов интерфейса"""
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Заголовок
        ttk.Label(
            main_frame,
            text="🛡️ Блокировщик Яндекс Браузера",
            font=("Segoe UI", 18, "bold")
        ).pack(pady=(0, 5))
        
        ttk.Label(
            main_frame,
            text=f"Версия {__version__} • Моментальная блокировка через реестр",
            font=("Segoe UI", 9),
            foreground="gray"
        ).pack(pady=(0, 15))
        
        # Статус администратора
        if is_admin():
            admin_frame = ttk.Frame(main_frame)
            admin_frame.pack(fill=tk.X, pady=5)
            ttk.Label(
                admin_frame,
                text="✅ Права администратора получены",
                font=("Segoe UI", 10, "bold"),
                foreground="green"
            ).pack()
        else:
            admin_frame = ttk.Frame(main_frame)
            admin_frame.pack(fill=tk.X, pady=5)
            ttk.Label(
                admin_frame,
                text="❌ Требуются права администратора!",
                font=("Segoe UI", 10, "bold"),
                foreground="red"
            ).pack()
        
        # Разделитель
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=15)
        
        # Статус блокировки
        status_frame = ttk.LabelFrame(main_frame, text="📊 Статус блокировки", padding="15")
        status_frame.pack(fill=tk.X, pady=10)
        
        # Индикатор
        indicator_frame = ttk.Frame(status_frame)
        indicator_frame.pack(pady=10)
        
        self.indicator = tk.Canvas(indicator_frame, width=80, height=80, highlightthickness=0)
        self.indicator.pack()
        self.indicator_circle = self.indicator.create_oval(
            10, 10, 70, 70, fill="gray", outline="darkgray", width=3
        )
        
        self.status_var = tk.StringVar(value="Проверка...")
        self.status_label = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            font=("Segoe UI", 14, "bold")
        )
        self.status_label.pack(pady=5)
        
        self.blocked_count_var = tk.StringVar(value="")
        ttk.Label(
            status_frame,
            textvariable=self.blocked_count_var,
            font=("Segoe UI", 10),
            foreground="gray"
        ).pack()
        
        # Кнопки управления
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(pady=20)
        
        self.block_button = tk.Button(
            button_frame,
            text="🔒 ЗАБЛОКИРОВАТЬ",
            font=("Segoe UI", 12, "bold"),
            bg="#dc3545",
            fg="white",
            width=18,
            height=2,
            cursor="hand2",
            command=self._on_block
        )
        self.block_button.pack(side=tk.LEFT, padx=10)
        
        self.unblock_button = tk.Button(
            button_frame,
            text="🔓 РАЗБЛОКИРОВАТЬ",
            font=("Segoe UI", 12, "bold"),
            bg="#28a745",
            fg="white",
            width=18,
            height=2,
            cursor="hand2",
            command=self._on_unblock
        )
        self.unblock_button.pack(side=tk.LEFT, padx=10)
        
        # Дополнительные действия
        extra_frame = ttk.Frame(main_frame)
        extra_frame.pack(pady=10)
        
        ttk.Button(
            extra_frame,
            text="💀 Завершить все процессы Яндекса",
            command=self._on_kill_processes
        ).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(
            extra_frame,
            text="🔄 Обновить статус",
            command=self._update_status
        ).pack(side=tk.LEFT, padx=5)
        
        # Список блокируемых файлов
        files_frame = ttk.LabelFrame(main_frame, text="📁 Блокируемые файлы", padding="10")
        files_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        # Создаём список с прокруткой
        list_frame = ttk.Frame(files_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.files_listbox = tk.Listbox(
            list_frame,
            font=("Consolas", 10),
            height=6,
            yscrollcommand=scrollbar.set
        )
        self.files_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.files_listbox.yview)
        
        # Информация
        ttk.Label(
            main_frame,
            text="ℹ️ Блокировка работает через реестр Windows (IFEO).\n"
                 "Браузер не сможет запуститься вообще — это моментальная блокировка.",
            font=("Segoe UI", 9),
            foreground="gray",
            justify=tk.CENTER
        ).pack(pady=10)
    
    def _update_status(self):
        """Обновляет статус блокировки в интерфейсе"""
        executables = self.config.blocked_executables
        status = RegistryBlocker.get_status(executables)
        blocked_count = sum(1 for v in status.values() if v)
        total_count = len(executables)
        
        # Обновляем список файлов
        self.files_listbox.delete(0, tk.END)
        for exe, is_blocked in status.items():
            icon = "🔒" if is_blocked else "🔓"
            state = "ЗАБЛОКИРОВАН" if is_blocked else "не заблокирован"
            self.files_listbox.insert(tk.END, f"  {icon}  {exe} — {state}")
        
        # Обновляем индикатор и статус
        if blocked_count == total_count:
            # Полная блокировка
            self.indicator.itemconfig(self.indicator_circle, fill="#dc3545", outline="#c82333")
            self.status_var.set("🔴 ЗАБЛОКИРОВАН")
            self.status_label.configure(foreground="#dc3545")
            self.block_button.config(state=tk.DISABLED)
            self.unblock_button.config(state=tk.NORMAL)
        elif blocked_count > 0:
            # Частичная блокировка
            self.indicator.itemconfig(self.indicator_circle, fill="#ffc107", outline="#e0a800")
            self.status_var.set("🟡 ЧАСТИЧНО")
            self.status_label.configure(foreground="#ffc107")
            self.block_button.config(state=tk.NORMAL)
            self.unblock_button.config(state=tk.NORMAL)
        else:
            # Не заблокирован
            self.indicator.itemconfig(self.indicator_circle, fill="#6c757d", outline="#545b62")
            self.status_var.set("⚪ НЕ ЗАБЛОКИРОВАН")
            self.status_label.configure(foreground="#6c757d")
            self.block_button.config(state=tk.NORMAL)
            self.unblock_button.config(state=tk.DISABLED)
        
        self.blocked_count_var.set(f"Заблокировано: {blocked_count} из {total_count}")
    
    def _on_block(self):
        """Обработчик кнопки блокировки"""
        if not is_admin():
            messagebox.showerror(
                "Ошибка",
                "Для блокировки требуются права администратора!\n\n"
                "Перезапустите программу с правами администратора."
            )
            return
        
        # Сначала завершаем все процессы
        killed = ProcessKiller.kill_all_yandex()
        
        # Затем блокируем в реестре
        success, messages = RegistryBlocker.block_all(self.config.blocked_executables)
        
        self._update_status()
        
        # Показываем результат
        result_text = "\n".join(f"• {m}" for m in messages)
        if killed:
            result_text += f"\n\n💀 Завершено процессов: {len(killed)}"
        
        if success:
            messagebox.showinfo(
                "✅ Заблокировано",
                f"Яндекс Браузер заблокирован!\n\n{result_text}\n\n"
                "Браузер больше не сможет запуститься."
            )
        else:
            messagebox.showwarning(
                "⚠️ Частичная блокировка",
                f"Не все файлы заблокированы:\n\n{result_text}"
            )
    
    def _on_unblock(self):
        """Обработчик кнопки разблокировки"""
        if not is_admin():
            messagebox.showerror(
                "Ошибка",
                "Для разблокировки требуются права администратора!\n\n"
                "Перезапустите программу с правами администратора."
            )
            return
        
        success, messages = RegistryBlocker.unblock_all(self.config.blocked_executables)
        
        self._update_status()
        
        result_text = "\n".join(f"• {m}" for m in messages)
        
        if success:
            messagebox.showinfo(
                "✅ Разблокировано",
                f"Яндекс Браузер разблокирован!\n\n{result_text}\n\n"
                "Теперь браузер можно запускать."
            )
        else:
            messagebox.showwarning(
                "⚠️ Частичная разблокировка",
                f"Не все файлы разблокированы:\n\n{result_text}"
            )
    
    def _on_kill_processes(self):
        """Завершает все процессы Яндекс Браузера"""
        killed = ProcessKiller.kill_all_yandex()
        
        if killed:
            messagebox.showinfo(
                "💀 Процессы завершены",
                f"Завершено процессов: {len(killed)}\n\n" +
                "\n".join(f"• {name}" for name in killed[:10]) +
                ("\n..." if len(killed) > 10 else "")
            )
        else:
            messagebox.showinfo(
                "ℹ️ Информация",
                "Процессы Яндекс Браузера не найдены."
            )
    
    def _on_close(self):
        """Обработчик закрытия окна"""
        self.root.destroy()
    
    def run(self):
        """Запуск приложения"""
        self.root.mainloop()


def main():
    """Точка входа"""
    # Проверяем права администратора
    if not is_admin():
        with temp_tk_root():
            result = messagebox.askyesno(
                "Права администратора",
                "Для работы блокировщика требуются права администратора.\n\n"
                "Это необходимо для изменения реестра Windows.\n\n"
                "Запустить с правами администратора?"
            )
        
        if result:
            if run_as_admin():
                sys.exit(0)
            else:
                with temp_tk_root():
                    messagebox.showerror(
                        "Ошибка",
                        "Не удалось получить права администратора."
                    )
                sys.exit(1)
    
    # Запускаем приложение
    app = YandexBlockerApp()
    app.run()


if __name__ == "__main__":
    main()