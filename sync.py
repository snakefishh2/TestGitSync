import os
import hashlib
import socket
import subprocess
from datetime import datetime

# --- Настройки ---
LOCAL_REPO = r"Конфиги"  # Путь к локальному клону репозитория
BRANCH = "master"
LOCAL_PREFIX = "LOCAL_"
REMOTE_PREFIX = "REMOTE_"
LOG_FILE = os.path.join(LOCAL_REPO, "sync_log.txt")
HASH_FILE = os.path.join(LOCAL_REPO, ".file_hashes.txt")

# --- Прокси (опционально, работает для всего репозитория) ---
HTTP_PROXY = ""   # пример: "http://username:password@proxyserver:port"
HTTPS_PROXY = ""  # пример: "http://username:password@proxyserver:port"

# --- Сообщение коммита ---
computer_name = socket.gethostname()
current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
COMMIT_MESSAGE = f"Автообновление [{computer_name} {current_time}]"

# --- Логирование ---
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_message + "\n")

# --- Проверка интернета ---
def is_internet_available(host="github.com", port=443, timeout=5):
    try:
        socket.create_connection((host, port), timeout=timeout)
        return True
    except OSError:
        return False

# --- Вызов git-команды ---
def run_git_command(args, check=True):
    result = subprocess.run(
        ["git"] + args,
        cwd=LOCAL_REPO,
        text=True,
        capture_output=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Ошибка git {' '.join(args)}:\n{result.stderr}")
    return result.stdout.strip()

# --- Работа с файлами ---
def get_all_files(root_dir):
    all_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            full_path = os.path.relpath(os.path.join(dirpath, f), root_dir)
            all_files.append(full_path)
    return all_files

def compute_file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def load_hashes():
    if not os.path.exists(HASH_FILE):
        return {}
    hashes = {}
    with open(HASH_FILE, "r", encoding="utf-8") as f:
        for line in f:
            path, h = line.strip().split("||")
            hashes[path] = h
    return hashes

def save_hashes(hashes):
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        for path, h in hashes.items():
            f.write(f"{path}||{h}\n")

# --- Сохранение конфликтных файлов ---
def save_conflict_versions(conflicted_files):
    for file_path in conflicted_files:
        full_path = os.path.join(LOCAL_REPO, file_path)
        dir_path = os.path.dirname(full_path)

        # Локальная версия (HEAD)
        local_path = os.path.join(dir_path, f"{LOCAL_PREFIX}{os.path.basename(file_path)}")
        with open(local_path, "wb") as f:
            subprocess.run(["git", "show", f"HEAD:{file_path}"], cwd=LOCAL_REPO, stdout=f)
        log(f"Сохранена локальная версия: {local_path}")

        # Удалённая версия (origin/BRANCH)
        remote_path = os.path.join(dir_path, f"{REMOTE_PREFIX}{os.path.basename(file_path)}")
        with open(remote_path, "wb") as f:
            subprocess.run(["git", "show", f"origin/{BRANCH}:{file_path}"], cwd=LOCAL_REPO, stdout=f)
        log(f"Сохранена удалённая версия: {remote_path}")

# --- Синхронизация ---
def sync_repo():
    # Настройка прокси (если указано)
    if HTTP_PROXY:
        run_git_command(["config", "http.proxy", HTTP_PROXY])
    if HTTPS_PROXY:
        run_git_command(["config", "https.proxy", HTTPS_PROXY])

    previous_hashes = load_hashes()
    changed = False

    # --- Pull (обычный) ---
    if is_internet_available():
        try:
            result = subprocess.run(
                ["git", "pull", "origin", BRANCH],
                cwd=LOCAL_REPO,
                text=True,
                capture_output=True
            )
            if result.returncode != 0:
                if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
                    log("Обнаружены конфликты при pull. Сохраняем обе версии...")
                    conflicts = subprocess.run(
                        ["git", "diff", "--name-only", "--diff-filter=U"],
                        cwd=LOCAL_REPO,
                        text=True,
                        capture_output=True
                    ).stdout.splitlines()
                    save_conflict_versions(conflicts)
                    run_git_command(["merge", "--abort"], check=False)
                else:
                    log(f"Ошибка при pull: {result.stderr}")
            else:
                log("Pulled latest changes from GitHub.")
        except Exception as e:
            log(f"Ошибка при pull: {e}")
    else:
        log("Интернет недоступен: pull пропущен.")

    # --- Проверка файлов ---
    all_files = get_all_files(LOCAL_REPO)
    new_hashes = previous_hashes.copy()
    for rel_path in all_files:
        full_path = os.path.join(LOCAL_REPO, rel_path)
        if not os.path.isfile(full_path):
            continue
        file_hash = compute_file_hash(full_path)
        if rel_path not in previous_hashes or previous_hashes[rel_path] != file_hash:
            try:
                run_git_command(["add", rel_path])
                log(f"Файл изменён или новый: {rel_path}")
                new_hashes[rel_path] = file_hash
                changed = True
            except RuntimeError as e:
                log(f"Ошибка при добавлении {rel_path}: {e}")

    # --- Commit ---
    if changed:
        try:
            run_git_command(["commit", "-m", COMMIT_MESSAGE], check=False)
            log("Изменения закоммичены локально.")
        except RuntimeError as e:
            log(f"Ошибка при commit: {e}")

        # --- Pull --rebase + Push ---
        if is_internet_available():
            try:
                result = subprocess.run(
                    ["git", "pull", "--rebase", "origin", BRANCH],
                    cwd=LOCAL_REPO,
                    text=True,
                    capture_output=True
                )
                if result.returncode != 0:
                    if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
                        log("Конфликты при rebase. Сохраняем обе версии...")
                        conflicts = subprocess.run(
                            ["git", "diff", "--name-only", "--diff-filter=U"],
                            cwd=LOCAL_REPO,
                            text=True,
                            capture_output=True
                        ).stdout.splitlines()
                        save_conflict_versions(conflicts)
                        run_git_command(["rebase", "--abort"], check=False)
                    else:
                        log(f"Ошибка при pull --rebase: {result.stderr}")
                else:
                    run_git_command(["push", "origin", BRANCH])
                    log("Изменения успешно отправлены на GitHub.")
            except RuntimeError as e:
                log(f"Не удалось отправить изменения на GitHub: {e}")
        else:
            log("Интернет недоступен: push пропущен.")

        save_hashes(new_hashes)
    else:
        log("Нет новых изменений для коммита.")

if __name__ == "__main__":
    sync_repo()
