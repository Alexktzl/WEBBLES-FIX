import eel
import logging
from pathlib import Path
import subprocess

# Настройка логов
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('WebblesApp')

# Инициализируем Eel с папкой, где лежат index.html и другие файлы
# Убедитесь, что папка называется 'web' и находится в корне проекта
eel.init('web')

# Импорт контроллера Webble Fix
from core.controller import Controller
ctrl = Controller()

@eel.expose
def select_folder():
    """
    Открывает диалог выбора папки через PowerShell (Windows).
    Возвращает полный путь к папке или None, если пользователь отменил выбор.
    """
    try:
        ps_script = """
        Add-Type -AssemblyName System.Windows.Forms
        $folder = New-Object System.Windows.Forms.FolderBrowserDialog
        $folder.Description = "Выберите папку проекта"
        $folder.ShowDialog() | Out-Null
        $folder.SelectedPath
        """
        result = subprocess.run(
            ['powershell', '-Command', ps_script],
            capture_output=True,
            text=True,
            timeout=30
        )
        path = result.stdout.strip()
        if path:
            return str(Path(path).resolve())
        return None
    except Exception as e:
        logger.error(f"Ошибка при выборе папки: {e}")
        return None

@eel.expose
def run_pipeline(project_path_str, language):
    """
    Запускает конвейер исправления.
    Принимает путь и язык, возвращает словарь с результатами.
    """
    # Tech debt audit 2026-06-21, #6: путь от JS-стороны раньше не проверялся
    # вовсе. Не сужаем до "разрешённого корня" (легитимный сценарий — любой
    # проект пользователя), но требуем существующую директорию вместо падения
    # глубже внутри ctrl.run_pipeline с менее понятной ошибкой.
    try:
        project_path = Path(project_path_str).resolve(strict=True)
    except (OSError, RuntimeError) as e:
        logger.warning(f"run_pipeline: путь не резолвится: {e}")
        return {'status': 'error', 'message': f'Путь к проекту не найден: {e}'}
    if not project_path.is_dir():
        logger.warning(f"run_pipeline: путь не директория: {project_path}")
        return {'status': 'error', 'message': f'Путь к проекту не является директорией: {project_path}'}
    config = ctrl.load_config()
    logger.info(f"Запуск конвейера для {project_path} (язык: {language})")

    try:
        result = ctrl.run_pipeline(project_path, language, config)
        return {
            'status': 'ok',
            'data': {
                'initial_errors': result.get('initial_error_count', 0),
                'final_errors': result.get('final_error_count', 0),
                'baseline_remaining': result.get('baseline_remaining', result.get('final_error_count', 0)),
                'current_total_errors': result.get('current_total_errors'),
                'accepted_patches': result.get('accepted_patches', 0),
                'remaining_errors': result.get('remaining_errors', [])
            }
        }
    except Exception as e:
        logger.exception("Ошибка в run_pipeline")
        return {'status': 'error', 'message': str(e)}

if __name__ == '__main__':
    # Запускаем в Edge с автоматическим поиском порта.
    # Если Edge недоступен, откатываемся на браузер по умолчанию.
    try:
        eel.start('index.html', mode='edge', size=(1280, 900), port=0)
    except EnvironmentError:
        logger.warning("Edge не найден, запускаем в браузере по умолчанию.")
        eel.start('index.html', mode='default', size=(1280, 900), port=0)