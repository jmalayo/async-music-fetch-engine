"""Crea una cuenta de la web (no hay registro público).

    python scripts/create_user.py <usuario> [--admin] [--cuota-gb 1]

Pide la contraseña sin mostrarla. Con Docker:

    docker compose exec api python ../scripts/create_user.py <usuario>
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core import auth  # noqa: E402
from infrastructure.persistence.database import SessionLocal, init_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Crea una cuenta de owlo.")
    parser.add_argument("usuario")
    parser.add_argument("--admin", action="store_true", help="puede administrar el almacenamiento de todos")
    parser.add_argument("--cuota-gb", type=float, default=1.0, help="almacenamiento del usuario en GB (por defecto 1)")
    args = parser.parse_args()

    password = getpass.getpass("Contraseña: ")
    if password != getpass.getpass("Repite la contraseña: "):
        sys.exit("Las contraseñas no coinciden.")

    init_db()
    with SessionLocal() as db:
        try:
            user = auth.create_user(db, args.usuario, password, is_admin=args.admin, quota_bytes=int(args.cuota_gb * 1024 ** 3))
        except ValueError as e:
            sys.exit(str(e))
    print(f"Cuenta «{user.username}» creada ({args.cuota_gb:g} GB{', administrador' if args.admin else ''}).")


if __name__ == "__main__":
    main()
