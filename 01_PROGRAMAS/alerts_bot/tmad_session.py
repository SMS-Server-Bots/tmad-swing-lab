from pathlib import Path
from playwright.sync_api import sync_playwright

OUT_FILE = (Path(__file__).resolve().parents[2] / "02_CONFIG" / "tmad_session.json")
TMAD_URL = "https://tmad.app"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=100,
        )

        context = browser.new_context()
        page = context.new_page()

        print("[INFO] Abriendo TMAD...")
        page.goto(TMAD_URL, wait_until="domcontentloaded")

        print("")
        print("====================================================")
        print(" Inicia sesión manualmente con la NUEVA cuenta TMAD ")
        print(" Cuando estés dentro del dashboard, vuelve aquí      ")
        print(" y pulsa ENTER.                                     ")
        print("====================================================")
        print("")

        input("Pulsa ENTER cuando ya estés logueado en TMAD... ")

        context.storage_state(path=str(OUT_FILE))

        print(f"[OK] Sesión exportada correctamente en: {OUT_FILE.resolve()}")

        browser.close()

if __name__ == "__main__":
    main()