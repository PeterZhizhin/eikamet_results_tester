import aiohttp
import argparse
import asyncio
import logging
import os
import sanic
import sanic.response
import selenium.webdriver
import telegram
import time

EIKAMET_MAIN_PAGE = "https://e-ikamet.goc.gov.tr/Ikamet/DevamEdenBasvuruGiris"
APPLICATION_FIELD_ID = "basvuruNo"
EMAIL_FIELD_ID = "ePosta"
PASSPORT_FIELD_ID = "pasaportBelgeNo"
CAPTCHA_FIELD_ID = "CaptchaInputText"
CAPTCHA_IMAGE_FIELD_ID = "CaptchaImage"
BUTTON_CSS_SELECTOR = "button.btn-login"
LOADING_TEXT_CSS_SELECTOR = "span.loading-text"

RESULT_ELEMENT_CSS_SELECTOR = "span.noty_text"

WRONG_CAPTCHA_ERROR_MESSAGE = "Image verification fails."
REFRESH_CAPTCHA_BUTTON_TITLE = "Click to refresh the image verification code."


APPLICATION_NUMBER = os.environ["APPLICATION_NUMBER"]
EMAIL = os.environ["EMAIL"]
PASSPORT_NUMBER = os.environ["PASSPORT_NUMBER"]

RUCAPTCHA_API_KEY = os.environ["RUCAPTCHA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_BOT_CHAT_ID = os.environ["TELEGRAM_BOT_CHAT_ID"]


logger = logging.getLogger(__name__)

app = sanic.Sanic(__name__)
app.config.REQUEST_TIMEOUT = 600
app.config.RESPONSE_TIMEOUT = 600
app.config.KEEP_ALIVE_TIMEOUT = 600


class SolveCaptchaException(Exception):
    pass


class CaptchaSolver:
    def __init__(self, captcha_image_base64):
        self.captcha_image_base64 = captcha_image_base64
        self.captcha_key = None


    async def solve_captcha(self):
        async with aiohttp.ClientSession() as session:
            logger.info("Sending captcha to rucaptcha")
            async with session.post("http://rucaptcha.com/in.php", data={
                'key': RUCAPTCHA_API_KEY,
                'method': 'base64',
                'body': self.captcha_image_base64,
                'numeric': 2,
                'min_len': 8,
                'max_len': 8,
                'language': 2,
                'json': 1}) as captcha_response:
                captcha_response_text = await captcha_response.text()
                logger.info(f"Got response from rucaptcha {captcha_response_text}")
                if not captcha_response.ok:
                    raise SolveCaptchaException("Got bad error code from RuCaptcha: {captcha_response} {captcha_response_text}")
                try:
                    captcha_response_json = await captcha_response.json()
                except json.JSONDecodeError:
                    logger.error(f"Could not parse JSON: {captcha_response_text}")
                    raise SolveCaptchaException(f"Could not parse in.php output JSON response: {captcha_response_text}")

                if captcha_response_json['status'] != 1:
                    raise SolveCaptchaException(f"Error from RuCaptcha: {captcha_response_json['request']}")

                self.captcha_key = captcha_response_json['request']
                return await self._wait_for_captcha_output(session)


    async def _wait_for_captcha_output(self, session):
        while True:
            logger.info("Waiting for captcha output")
            await asyncio.sleep(5)
            async with session.get("http://rucaptcha.com/res.php", params={
                'action': 'get',
                'key': RUCAPTCHA_API_KEY,
                'id': self.captcha_key,
                'json': 1
                }) as captcha_output_response:
                captcha_output_response_text = await captcha_output_response.text()
                logger.info(f"Captcha output: {captcha_output_response_text}")
                captcha_output_response = await captcha_output_response.json()
                if captcha_output_response['request'] == "CAPCHA_NOT_READY":
                    continue
                if captcha_output_response['request'].startswith("ERROR") or captcha_output_response['status'] != 1:
                    raise SolveCaptchaException(f"Got error while solving captcha: {captcha_output_response}")
                return captcha_output_response['request'].upper()


    async def report_good(self):
        assert self.captcha_key is not None
        async with aiohttp.ClientSession() as session:
            async with session.get("http://rucaptcha.com/res.php", params={
                'action': 'reportgood',
                'key': RUCAPTCHA_API_KEY,
                'id': self.captcha_key,
                }) as resp:
                return resp.status


    async def report_bad(self):
        assert self.captcha_key is not None
        async with aiohttp.ClientSession() as session:
            async with session.get("http://rucaptcha.com/res.php", params={
                'action': 'reportbad',
                'key': RUCAPTCHA_API_KEY,
                'id': self.captcha_key,
                }) as resp:
                return resp.status


def fill_element(driver, element_id, element_value):
    element = driver.find_element(by='id', value=element_id)
    element.clear()
    element.click()
    element.send_keys(element_value)


async def solve_eikamet_captcha(driver):
    bad_captcha_attempts = 0
    while True:
        captcha_image_element = driver.find_element(by='id', value=CAPTCHA_IMAGE_FIELD_ID)
        captcha_image_base64 = captcha_image_element.screenshot_as_base64

        captcha_solver = CaptchaSolver(captcha_image_base64)
        try:
            ikamet_captcha_text = await captcha_solver.solve_captcha()
        except SolveCaptchaException as error:
            if bad_captcha_attempts > 2:
                raise
            logger.error(f"Got error while solving captcha: {error}")
            refresh_button = driver.find_element(by='css selector', value=f"[title^='{REFRESH_CAPTCHA_BUTTON_TITLE}']")
            refresh_button.click()
            await asyncio.sleep(0.5) # Wait for refresh
            bad_captcha_attempts += 1
            continue

        logger.info(f"Solved captcha as {ikamet_captcha_text}")

        fill_element(driver, CAPTCHA_FIELD_ID, ikamet_captcha_text)

        login_button = driver.find_element(by='css selector', value=BUTTON_CSS_SELECTOR)
        login_button.click()

        # Wait for results to load
        await asyncio.sleep(5)

        full_screenshot = driver.find_element(by='tag name', value='body').screenshot_as_png

        try:
            result_value_element = driver.find_element(by='css selector', value=RESULT_ELEMENT_CSS_SELECTOR)
        except selenium.common.exceptions.NoSuchElementException:
            logger.error("Did not find noty_text element on the page")
            result_text = "Unknown status"
        else:
            result_text = result_value_element.text.strip()
            result_value_element.click()
            if result_text == WRONG_CAPTCHA_ERROR_MESSAGE:
                await captcha_solver.report_bad()
                continue

        await captcha_solver.report_good()
        return result_text, full_screenshot


def window_resize(driver):
    required_width = driver.execute_script('return document.body.parentNode.scrollWidth')
    required_height = driver.execute_script('return document.body.parentNode.scrollHeight')
    driver.set_window_size(required_width, required_height + 100)


async def get_ikamet_status(driver):
    logger.info("Getting e-ikamet status")
    driver.get(EIKAMET_MAIN_PAGE)
    window_resize(driver)
    fill_element(driver, APPLICATION_FIELD_ID, APPLICATION_NUMBER)
    fill_element(driver, EMAIL_FIELD_ID, EMAIL)
    fill_element(driver, PASSPORT_FIELD_ID, PASSPORT_NUMBER)

    result, result_screenshot = await solve_eikamet_captcha(driver)
    logger.info(f"Result output: {result}")
    return result, result_screenshot


async def send_message_to_tg(message, screenshot):
    bot = telegram.Bot(TELEGRAM_BOT_TOKEN)
    async with bot:
        await bot.send_message(chat_id=TELEGRAM_BOT_CHAT_ID, text=message)
        await bot.send_photo(chat_id=TELEGRAM_BOT_CHAT_ID, photo=screenshot)


@app.route("/", methods=["GET", "POST"])
async def handle_request(request):
    options = selenium.webdriver.ChromeOptions()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    with selenium.webdriver.Chrome(options=options) as driver:
        ikamet_status, screenshot = await get_ikamet_status(driver)
        await send_message_to_tg(ikamet_status, screenshot)
    return sanic.response.text(ikamet_status)


def main():
    logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.INFO)
    logger.info("Starting")
    app.run(host='0.0.0.0', port=os.environ['PORT'], motd=False)


if __name__ == "__main__":
    main()
