import pymysql


class Storage:
    def __init__(self, host, port, user, password, database):
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
            'INSERT INTO `message` (`level`, `content`, `domain`) VALUES (%s, %s, %s)', (level, message, domain)
        )
        cur.close()
        conn.commit()
        conn.close()

    def link_chat(self, domain, chat_id):
        with pymysql.connect(**self.conn_args) as conn:
            with conn.cursor() as cur:
                if isinstance(domain, str):
                    domain = self.domain_id(domain, cur)
                cur.execute('SELECT * FROM `tg` WHERE `domain`=%s AND `cid`=%s', (domain, chat_id))
                fetched = cur.fetchall()
                is_new = False
                if len(fetched) == 0:
                    is_new = True
                    cur.execute('INSERT INTO `tg` (`domain`, `cid`) VALUES (%s, %s)', (domain, chat_id))
            conn.commit()
        return domain, chat_id, is_new

    def pull(self, domain, level_range, past):
        conn = pymysql.connect(**self.conn_args)
        cur = conn.cursor()
        if isinstance(domain, str):
            domain = self.domain_id(domain, cur)
        low, high = level_range
        cur.execute(
            'SELECT `id`, `content` FROM `message` '
            'WHERE domain=%s AND level >= %s AND level <= %s AND ts >= UNIX_TIMESTAMP() - %s',
            (domain, low, high, past)
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
