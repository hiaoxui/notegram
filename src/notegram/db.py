from typing import Tuple
import time

import psycopg


class Storage:
    def __init__(self, host, port, user, password, database):
        self.conn_args = {'host': host, 'user': user, 'password': password, 'dbname': database, 'port': port}

    @staticmethod
    def domain_id(domain, cursor) -> int:
        cursor.execute('SELECT id FROM domain WHERE name = %s', (domain,))
        q_rst = cursor.fetchall()
        if len(q_rst) == 0:
            cursor.execute('INSERT INTO domain (name) VALUES (%s)', (domain,))
            cursor.execute('SELECT id FROM domain WHERE name = %s', (domain,))
            q_rst = cursor.fetchall()
        domain_id = q_rst[0][0]
        return domain_id

    def post(self, level: int, domain: str | int, message: str):
        with psycopg.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                if isinstance(domain, str):
                    domain = self.domain_id(domain=domain, cursor=cur)
                cur.execute(
                    query='INSERT INTO message (level, content, domain, ts) VALUES (%s, %s, %s, %s)', params=(level, message, domain, int(time.time()))
                )
            conn.commit()

    def link_chat(self, domain: str | int, chat_id: int) -> Tuple[int, int, bool]:
        with psycopg.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                if isinstance(domain, str):
                    domain = self.domain_id(domain=domain, cursor=cur)
                cur.execute('SELECT 1 FROM tg WHERE domain = %s AND cid = %s', params=(domain, chat_id))
                fetched = cur.fetchall()
                is_new = False
                if len(fetched) == 0:
                    is_new = True
                    cur.execute('INSERT INTO tg (domain, cid) VALUES (%s, %s)', params=(domain, chat_id))
            conn.commit()
        return domain, chat_id, is_new

    def pull(self, domain: int | str, level_range: Tuple[int, int], past: int):
        with psycopg.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                if isinstance(domain, str):
                    domain = self.domain_id(domain=domain, cursor=cur)
                low, high = level_range
                ts = int(time.time() - past)
                cur.execute(
                    query='SELECT id, content FROM message WHERE domain = %s AND level >= %s AND level <= %s AND ts >= %s',
                    params=(domain, low, high, ts)
                )
                rst = cur.fetchall()
        return rst

    def fetch_tg(self):
        with psycopg.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT cid, domain, report_level, base_level FROM public.tg')
                rst = cur.fetchall()
        return rst

    def cid2domain(self, cid):
        with psycopg.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT domain FROM tg WHERE cid = %s', (cid,))
                rst = cur.fetchall()
        return [item[0] for item in rst]
