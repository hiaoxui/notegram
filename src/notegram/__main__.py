#!/usr/bin/env python

from argparse import ArgumentParser
import logging

from notegram.bot import TelegramBot

logger = logging.getLogger('notifier')


def run():
    parser = ArgumentParser()
    parser.add_argument('command', type=str, choices=['bot'])
    args = parser.parse_args()
    if args.command == 'bot':
        logger.warning('start to run bot.')
        bot = TelegramBot()
        bot.run()
    else:
        logger.error(f'unknown command: {args.command}')


if __name__ == '__main__':
    run()
