import pymysql
import time

from notifier.util import load_config


class Storage:
    def __init__(
            self, host, port, user, password, database
    ):
        self.conn_args = {'host': host, 'user': user, 'password': password, 'database': database, 'port': port}

    @staticmethod
    def domain_id(domain, cursor):
        cursor.execute('SELECT `id` from `domain` where `name` = %s', domain)
        q_rst = cursor.fetchall()
        if len(q_rst) == 0:
            cursor.execute('INSERT INTO `domain` (`name`) VALUES (%s)', domain)
            cursor.execute('SELECT `id` from `domain` where `name` = %s', domain)
            q_rst = cursor.fetchall()
        domain_id = q_rst[0][0]
        return domain_id

    def post(self, level, domain, message):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        if isinstance(domain, str):
            domain = self.domain_id(domain, cur)
        cur.execute(
            'INSERT INTO `message` (`level`, `ts`, `content`, `domain`) VALUES (%s, %s, %s, %s)',
            (level, int(time.time()), message, domain)
        )
        cur.close()
        conn.commit()
        conn.close()

    def link_chat(self, domain, chat_id):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        if isinstance(domain, str):
            domain = self.domain_id(domain, cur)
        cur.execute('INSERT INTO `tg` (`domain`, `cid`) VALUES (%s, %s)', (domain, chat_id))
        cur.close()
        conn.commit()
        conn.close()

    def pull(self, domain, level, past):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        if isinstance(domain, str):
            domain = self.domain_id(domain, cur)
        if level is None:
            cur.execute(
                'SELECT `id`, `content` FROM `message` WHERE domain=%s AND level >= 10 AND ts >= %s',
                (domain, int(time.time() - past))
            )
        else:
            cur.execute(
                'SELECT `id`, `content` FROM `message` WHERE domain=%s AND level=%s AND ts >= %s',
                (domain, level, int(time.time() - past))
            )
        rst = cur.fetchall()
        cur.close()
        conn.close()
        return rst

    def fetch_tg(self):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        cur.execute('SELECT `cid`, `domain`, `report_level`, `base_level` FROM `tg`')
        rst = cur.fetchall()
        cur.close()
        conn.close()
        return rst

    def cid2domain(self, cid):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        cur.execute('SELECT `domain` FROM `tg` WHERE `cid` = %s', cid)
        rst = cur.fetchall()
        cur.close()
        conn.close()
        return [item[0] for item in rst]
