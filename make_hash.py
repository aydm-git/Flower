from getpass import getpass

from werkzeug.security import generate_password_hash

password = getpass("Новый пароль: ")
if len(password) < 8:
    raise SystemExit("Пароль слишком короткий (минимум 8 символов).")
if password != getpass("Повтори пароль: "):
    raise SystemExit("Пароли не совпали.")

print("\nДобавь эту строку в .env:\n")
print(f"ADMIN_PASSWORD_HASH='{generate_password_hash(password)}'")
