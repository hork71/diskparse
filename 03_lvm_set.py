import asyncio
from collections import Counter
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


async def get_required_disks(session, url, groep):
    """Return the current required_disks of the group in Puppet, {} when it isn't set, or None on an error."""
    async with session.get(f"{url}/{groep['id']}", ssl=False) as response:
        if response.status >= 400:
            tekst = await response.text()
            logging.error(f"{tijdstip()},{groep['name']},{groep['id']},GET {response.status}, {tekst}")
            return None
        groepinfo = await response.json()

    return (groepinfo.get('variables') or {}).get('required_disks') or {}


async def post_variabelen(session, url, groep, variabelen):
    os_data = {
      'variables': variabelen
    }

    async with session.post(f"{url}/{groep['id']}", json=os_data, ssl=False) as response:
        if response.status >= 400:
            tekst = await response.text()
            logging.error(f"{tijdstip()},{groep['name']},{groep['id']},POST {response.status}, {tekst}")
            return False

    return True


async def fetch_data(session, url, groep, semaphore, dry_run):
    if 'required_disks' not in groep:
        return 'skipped'

    async with semaphore:
        try:
            return await update_groep(session, url, groep, dry_run)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error(f"{tijdstip()},{groep['name']},{groep['id']},{type(e).__name__}, {e}")
            return 'failed'


async def update_groep(session, url, groep, dry_run):
    nieuw = groep['required_disks'] or {}

    # compare with the current value in Puppet, not with the moment 02_lvm_info.py ran
    huidig = await get_required_disks(session, url, groep)
    if huidig is None:
        return 'failed'

    if huidig == nieuw:
        logging.info(f"{tijdstip()},{groep['name']},{groep['id']},unchanged, required_disks = {huidig}")
        return 'unchanged'

    if dry_run:
        logging.info(f"{tijdstip()},{groep['name']},{groep['id']},dry-run, required_disks {huidig} => {nieuw}")
        return 'changed'

    # remove the current value first, so the new value replaces it instead of being merged into it
    if not await post_variabelen(session, url, groep, {'required_disks': None}):
        return 'failed'

    # an empty required_disks means the group no longer has disk information, so leave it removed
    if nieuw:
        if not await post_variabelen(session, url, groep, {'required_disks': nieuw}):
            return 'failed'

    logging.info(f"{tijdstip()},{groep['name']},{groep['id']},required_disks {huidig} => {nieuw}")
    return 'changed'


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
            resultaten = await asyncio.gather(*tasks)

        totalen = Counter(resultaten)
        logging.info(f"{tijdstip()},{'dry-run, ' if dry_run else ''}changed: {totalen['changed']}, unchanged: {totalen['unchanged']}, failed: {totalen['failed']}, skipped: {totalen['skipped']}")

        if totalen['failed']:
            sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
