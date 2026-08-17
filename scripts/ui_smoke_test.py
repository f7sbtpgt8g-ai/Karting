"""One-off Playwright smoke test: upload the synthetic fixture into the
running Streamlit app and check the headline view renders without error.
Not part of the pytest suite -- run manually against a live `streamlit run app.py`.
"""
import os
import sys

from playwright.sync_api import sync_playwright

FIXTURE = os.path.abspath("tests/fixtures/synthetic_session.tsv")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page()
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto("http://localhost:8501", wait_until="networkidle")
        page.wait_for_selector("text=Upload Unipro TSV export(s)", timeout=15000)

        file_input = page.locator('input[type="file"]')
        file_input.set_input_files(FIXTURE)

        page.wait_for_selector("text=Top 3 Focus Areas", timeout=30000)
        page.wait_for_timeout(3000)  # let charts render

        page.screenshot(path="/tmp/karting_ui_headline.png", full_page=True)

        page.get_by_role("tab", name="Track Map").click()
        page.wait_for_timeout(2000)
        page.screenshot(path="/tmp/karting_ui_map.png", full_page=True)

        page.get_by_role("tab", name="Braking / RPM").click()
        page.wait_for_timeout(2000)
        page.screenshot(path="/tmp/karting_ui_braking.png", full_page=True)

        page.get_by_role("tab", name="Kart Setup").click()
        page.wait_for_timeout(2000)
        page.screenshot(path="/tmp/karting_ui_setup.png", full_page=True)

        body_text = page.inner_text("body")
        if "Traceback" in body_text or "Exception" in body_text or "streamlit.errors" in body_text:
            print("FOUND ERROR IN PAGE BODY")
            print(body_text[:4000])
            sys.exit(1)

        print("No errors found. Console errors:", errors[:10])
        browser.close()


if __name__ == "__main__":
    main()
