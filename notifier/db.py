import pymysql
import time

from notifier.util import load_config


class Storage:
    def __init__(
            self, host, port, user, password, database
    ):
        self.conn_args = {'host': host, 'user': user, 'password': password, 'database': database, 'port': port}

    def post(self, level, domain, message):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        cur.execute('SELECT `id` from `domain` where `name` = %s', domain)
        q_rst = cur.fetchall()
        if len(q_rst) == 0:
            cur.execute('INSERT INTO `domain` (`name`) VALUES (%s)', domain)
            cur.execute('SELECT `id` from `domain` where `name` = %s', domain)
            q_rst = cur.fetchall()
        domain_id = q_rst[0][0]
        cur.execute(
            'INSERT INTO `message` (`level`, `ts`, `content`, `domain`) VALUES (%s, %s, %s, %s)',
            (level, int(time.time()), message, domain_id)
        )
        cur.close()
        conn.commit()
        conn.close()
