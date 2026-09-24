"""Optional browser smoke test: start the site, install Playwright, then run this file."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def main():
    output = Path(__file__).resolve().parents[1] / "league_web"
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:7030", wait_until="networkidle")
        await page.locator("table").first.wait_for()
        assert await page.locator("tbody tr").count() == 10
        await page.screenshot(path=str(output / "preview-desktop.png"), full_page=True)
        await page.locator('.tabs a[href="#fixtures"]').click()
        await expect(page.locator(".match")).to_have_count(20)
        await page.locator("#division").select_option("Allied Division")
        await expect(page.locator(".match")).to_have_count(10)
        await page.locator("#round").select_option("1")
        await expect(page.locator(".match")).to_have_count(2)
        await page.locator("#query").fill("OFIN")
        await expect(page.locator(".match")).to_have_count(1)
        await page.locator("#calendar-view").click()
        await expect(page.locator(".day")).to_have_count(42)
        label = await page.locator(".calendar-bar h3").inner_text()
        await page.locator("#next").click()
        await expect(page.locator(".calendar-bar h3")).not_to_have_text(label)
        await page.locator('.tabs a[href="#results"]').click()
        await expect(page.locator("#content")).to_contain_text("No played matches")
        await page.locator('.tabs a[href="#rulebook"]').click()
        await expect(page.locator("#content")).to_contain_text("not been published")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator('.tabs a[href="#standings"]').click()
        await expect(page.locator("table")).to_have_count(2)
        await page.screenshot(path=str(output / "preview-mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.route("**/api/league", lambda route: route.fulfill(status=503, json={"error": "Unavailable"}))
        await page.locator("#refresh").click()
        await page.locator("#error").wait_for(state="visible")
        assert await page.locator("table").count() == 2
        assert not errors, errors
        print("PASS: desktop/mobile, four tabs, filters, calendar navigation, refresh failure; no JS errors.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
