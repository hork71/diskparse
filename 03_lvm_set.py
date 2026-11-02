import asyncio
from datetime import datetime
import json
import logging
import os
import sys
import aiohttp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(filename='lvm_set.log', filemode='w', encoding='utf-8', level=logging.INFO)

MAX_CONCURRENT = 10


def tijdstip():
    return datetime.today().strftime('%d-%m-%Y %H:%M:%S')


def read_groepen():
    with open('nodegroup_lvm.json') as rpfile:
        data = json.load(rpfile)

    return data


async def post_variabelen(session, url, groep, variabelen):
    os_data = {
      'variables': variabelen
    }

    async with session.post(f"{url}/{groep['id']}", json=os_data, ssl=False) as response:
        if response.status >= 400:
            tekst = await response.text()
            logging.error(f"{tijdstip()},{groep['name']},{groep['id']},{response.status}, {tekst}")
            return False

    return True


async def fetch_data(session, url, groep, semaphore, dry_run):
    if 'required_disks' not in groep:
        return

    async with semaphore:
        if dry_run:
            logging.info(f"{tijdstip()},{groep['name']},{groep['id']},dry-run, required_disks => {groep['required_disks']}")
            return

        # remove the current value first, so the new value replaces it instead of being merged into it
        if not await post_variabelen(session, url, groep, {'required_disks': None}):
            return

        # an empty required_disks means the group no longer has disk information, so leave it removed
        if groep['required_disks']:
            if not await post_variabelen(session, url, groep, {'required_disks': groep['required_disks']}):
                return

        logging.info(f"{tijdstip()},{groep['name']},{groep['id']},required_disks => {groep['required_disks']}")


async def main():
    dry_run = '--dry-run' in sys.argv[1:]

    groepen = read_groepen()
    if len(groepen) > 0:
        url = os.getenv('GROUPS_URL')
        headers = {
                'X-Authentication': os.getenv('PUPPET_TOKEN'),
                'content-type': 'application/json'
                }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [fetch_data(session, url, groep, semaphore, dry_run) for groep in groepen]
            await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
