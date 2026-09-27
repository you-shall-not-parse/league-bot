"""Optional browser smoke test: start the site, install Playwright, then run this file."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def main(base_url="http://127.0.0.1:7030"):
    output = Path(__file__).resolve().parents[1] / "league_web"
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base_url, wait_until="networkidle")
        report = await (await page.request.get(base_url.rstrip("/") + "/api/league")).json()
        await page.locator("table").first.wait_for()
        assert await page.locator("tbody tr").count() == 10
        await expect(page).to_have_title("The Allied Front")
        await expect(page.locator("tbody .clan-logo")).to_have_count(10)
        await page.wait_for_function("Array.from(document.querySelectorAll('.league-logo, .clan-logo')).every(img => img.complete && img.naturalWidth > 0)")
        for removed in ("SEASON 3", "Season 3", "PUBLIC LEAGUE BOARD", "COMMUNITY COMPETITION", "Follow the campaign.", "Independent community league"):
            await expect(page.locator("body")).not_to_contain_text(removed)
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
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        dated = [f for f in report["fixtures"] if f["division"] == "Allied Division" and f["round"] == 1
                 and "OFIN" in (f["a"], f["b"]) and (f.get("scheduled_at") or "").startswith(month)]
        if dated:
            await expect(page.locator(".calendar-event")).to_have_count(len(dated))
            await expect(page.locator(".calendar-event").first).to_contain_text("OFIN")
        label = await page.locator(".calendar-bar h3").inner_text()
        await page.locator("#next").click()
        await expect(page.locator(".calendar-bar h3")).not_to_have_text(label)
        await page.locator('.tabs a[href="#results"]').click()
        results = [f for f in report["fixtures"] if f["division"] == "Allied Division" and f["round"] == 1
                   and "OFIN" in (f["a"], f["b"]) and f["status"] in ("confirmed", "disputed", "score_submitted", "played_awaiting_score")]
        if results:
            await expect(page.locator(".match")).to_have_count(len(results))
            for result in results:
                if result["status"] == "confirmed":
                    await expect(page.locator(".score")).to_contain_text(f'{result["score_a"]} : {result["score_b"]}')
        else:
            await expect(page.locator("#content")).to_contain_text("No played matches")
        await page.locator('.tabs a[href="#rulebook"]').click()
        await expect(page.locator("#content")).to_contain_text("not been published")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator('.tabs a[href="#standings"]').click()
        await expect(page.locator("table")).to_have_count(2)
        await page.screenshot(path=str(output / "preview-mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await expect(page.locator('#metrics')).to_have_count(0)
        await expect(page.locator('.hero .eyebrow')).to_have_count(0)
        await expect(page.locator('.brand small')).to_have_text('HELL LET LOOSE CONSOLE COMPETITION')
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            await page.locator('.tabs a[href="#results"]').click()
            for card in await page.locator('.match-teams').all():
                boxes = [await item.bounding_box() for item in await card.locator(':scope > span').all()]
                centers = [b['y'] + b['height']/2 for b in boxes]
                assert max(centers) - min(centers) < 2, boxes
                assert await card.evaluate('(el) => el.scrollWidth <= el.clientWidth')
            winners = [f['a'] if f['score_a'] > f['score_b'] else f['b'] for f in results
                       if f['status'] == 'confirmed' and f.get('score_a') is not None
                       and f.get('score_b') is not None and f['score_a'] != f['score_b']]
            for winner in winners:
                if winner in report.get('clan_logos', {}):
                    assert await page.locator('.winner-art').evaluate_all(
                        '(images, src) => images.some(img => img.getAttribute("src") === src)',
                        report['clan_logos'][winner])
            await page.screenshot(path=str(output / f'preview-results-{width}.png'), full_page=True)
            await page.locator('.tabs a[href="#fixtures"]').click()
            await page.locator('#today').click()
            assert await page.locator('.calendar').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            if dated:
                await page.locator('.calendar-event').first.click()
                await expect(page.locator('#calendar-detail .match')).to_have_count(len(dated))
            await page.screenshot(path=str(output / f'preview-calendar-{width}.png'), full_page=True)
        await page.locator('.tabs a[href="#standings"]').click()
        await page.route("**/api/league", lambda route: route.fulfill(status=503, json={"error": "Unavailable"}))
        await page.locator("#refresh").click()
        await page.locator("#error").wait_for(state="visible")
        assert await page.locator("table").count() == 2
        assert not errors, errors
        print("PASS: desktop/mobile, four tabs, filters, calendar navigation, refresh failure; no JS errors.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
