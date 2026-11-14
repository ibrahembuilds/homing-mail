"""Run the campaign service on your own computer, without Docker or a mail server.

    python run.py                     web UI on http://localhost:8025/campaigns/
    python run.py --smtp-debug        also start a local mail catcher that prints
                                      every email instead of delivering it

Data is kept in ./data. Sign in with the password printed at start-up (or set
CAMPAIGNS_ADMIN_PASSWORD yourself).
"""
import argparse
import logging
import os
import secrets
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8025)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--no-worker', action='store_true', help='do not run the sending worker in this process')
    parser.add_argument('--smtp-debug', action='store_true', help='catch outgoing mail locally on port 8026')
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    os.environ.setdefault('CAMPAIGNS_DATA', os.path.join(here, 'data'))
    os.environ.setdefault('PUBLIC_URL', f'http://localhost:{args.port}')
    os.environ.setdefault('CAMPAIGNS_AUTH', 'password')
    if not os.environ.get('CAMPAIGNS_ADMIN_PASSWORD'):
        os.environ['CAMPAIGNS_ADMIN_PASSWORD'] = secrets.token_urlsafe(9)
        print(f'\n  Sign-in password for this run: {os.environ["CAMPAIGNS_ADMIN_PASSWORD"]}\n')
    if args.smtp_debug:
        os.environ['CAMPAIGNS_SMTP_HOST'] = '127.0.0.1'
        os.environ['CAMPAIGNS_SMTP_PORT'] = '8026'

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from app import create_app
    from app.config import Config
    from app import worker

    if args.smtp_debug:
        from tests.mailcatcher import MailCatcher
        catcher = MailCatcher(port=8026, echo=True)
        catcher.start()
        print('  Mail catcher on 127.0.0.1:8026 (emails are printed, not delivered)\n')

    app = create_app(Config())
    stop = threading.Event()
    if not args.no_worker:
        threading.Thread(target=worker.run, kwargs={'cfg': Config(), 'stop': stop}, daemon=True).start()
    print(f'  Open http://localhost:{args.port}/campaigns/\n')
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        stop.set()


if __name__ == '__main__':
    main()
