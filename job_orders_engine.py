from conf import config
from conf import database_setup

import sqlite3
import attrdict
import datetime
from poloniex import Poloniex


def _dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return attrdict.AttrDict(d)


conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = _dict_factory
cur = conn.cursor()

api_key = config.API_KEY
api_secret = config.API_SECRET
polo = Poloniex(api_key, api_secret)


def _update_status(transaction_id, status):
    cur.execute('UPDATE transactions SET status={} WHERE id={}'.format(
        status,
        transaction_id,
    ))
    conn.commit()


def process_buy_statuses(pair):
    to_enqueue = cur.execute(
        '''
        SELECT * FROM transactions WHERE pair="{pair}" and status={status} ORDER BY id DESC LIMIT 1;
        '''.format(
            pair=pair,
            status=config.TransactionStatus.TO_ENQUEUE,
        )
    ).fetchall()

    if not to_enqueue:
        return None

    to_enqueue = to_enqueue[0]

    # deleting old predictions
    cur.execute('DELETE FROM transactions WHERE status={status} and pair="{pair}"'.format(
        status=config.TransactionStatus.TO_ENQUEUE,
        pair=pair,
    ))
    conn.commit()

    latest_order = cur.execute(
        '''
        SELECT * FROM price WHERE pair="{}" ORDER BY id DESC LIMIT 1;
        '''.format(pair)
    ).fetchall()[0]

    balance = attrdict.AttrDict(polo.returnCompleteBalances()[config.get_pair_first_symbol(pair)])
    amount = balance.avalible * config.MAX_ORDER_PERCENT
    target_price = latest_order.buy * config.ORDERBOOK_FORCER_MOVE_PERCENT

    order_data = polo.buy(pair, target_price, amount)

    cur.execute(
        '''INSERT INTO transactions(
        id, ts, type, pair, status, amount, price
        ) VALUES (
        {id}, {ts}, {type}, "{pair}", {status}, {amount}, {price},
        ) '''.format(
            id=order_data.orderNumber,
            ts=to_enqueue.ts,
            type=config.TransactionType.BUY,
            pair=pair,
            status=config.TransactionStatus.ENQUEUED,
            amount=amount,
            price=target_price,
        )
    )

    conn.commit()
    return True


def process_stack_engine(pair):
    pair_orders = attrdict.AttrDict(polo.returnOpenOrders(currencyPair=pair))
    print("Pair orders", pair_orders)
    latest_order = cur.execute(
        '''
        SELECT * FROM price WHERE pair="{}" ORDER BY id DESC LIMIT 1;
        '''.format(pair)
    ).fetchall()[0]
    print(latest_order)

    for order_data in pair_orders:
        if order_data.type == 'buy':
            _process_buy_order(order_data, latest_order, pair)
        else:
            _process_sell_order(order_data, latest_order, pair)


def _process_buy_order(order_data, latest_order, pair):
    target_price = latest_order.buy * config.ORDERBOOK_FORCER_MOVE_PERCENT
    try:
        sql_order_data = cur.execute('SELECT * from transactions WHERE id={}'.format(order_data.orderNumber)).fetchall()[0]
        if sql_order_data.ts + config.DROP_BUY_ORDER_DELAY < datetime.datetime.utcnow().timestamp:
            print("Cancelling order {} BY TIME".format(sql_order_data))
            polo.cancelOrder(order_data.orderNumber)
            _update_status(order_data.orderNumber, config.TransactionStatus.CANCELLED)
            conn.commit()
            continue

        print('Trying to force order', sql_order_data)
        new_order = attrdict.AttrDict(polo.moveOrder(order_data.orderNumber, target_price))
        print('Forcing to target price success')
        cur.execute(
            '''UPDATE transactions SET price={}, id={} WHERE id={}'''.format(
                target_price, new_order.orderNumber, order_data.orderNumber
            )
        )
        conn.commit()
        print('Updating db success')
        return True
    except Exception as ex:
        print('Exception when forcing to target price', ex)
        return False


def _process_sell_order(order_data, latest_order, pair):
    # TODO: implement moving ON_STOP sell orders
    pass


def process_sell_statuses(pair):
    # add stop statuses
    pair_orders = attrdict.AttrDict(polo.returnOpenOrders(currencyPair=pair))

    for order_data in pair_orders:
        if order_data.type == 'buy':
            continue
        sql_order_data = cur.execute('SELECT * from transactions WHERE id={}'.format(order_data.orderNumber)).fetchall()[0]
        if sql_order_data.ts + config.STOP_TIME < datetime.datetime.utcnow().timestamp():
            _update_status(order_data.orderNumber, config.TransactionStatus.ON_STOP)

    # reseiving done buy trades & generating new sell transactions
    trades = polo.returnTradeHistory(currencyPair=pair)
    old_sell_trades_ids = [i[0] for i in cur.execute('SELECT id from trades;').fetchall()]
    new_trades = list(
        map(
            attrdict.AttrDict,
            filter(
                lambda tr: tr['globalTradeID'] not in old_sell_trades_ids and tr['type'] == 'buy',
                trades
            )
        )
    )
    balance = attrdict.AttrDict(polo.returnCompleteBalances()[config.get_pair_second_symbol(pair)]).avalible
    for trade in new_trades:
        can_sell_amount = balance * (trade.rate * config.STOP_PERCENT)
        target_price = trade.rate * config.STOP_PERCENT
        sell_amount = max(trade.amount, can_sell_amount)

        order_data = polo.sell(pair, target_price, sell_amount)

        cur.execute(
            '''INSERT INTO transactions(
            id, ts, type, pair, status, amount, price
            ) VALUES (
            {id}, {ts}, {type}, "{pair}", {status}, {amount}, {price},
            ) '''.format(
                id=order_data.orderNumber,
                ts=datetime.datetime.utcnow().timestamp(),
                type=config.TransactionType.SELL,
                pair=pair,
                status=config.TransactionStatus.ENQUEUED,
                amount=sell_amount,
                price=target_price,
            )
        )

        conn.commit()

        balance -= sell_amount


while True:

    for pair in config.PAIRS:
        try:
            process_buy_statuses(pair)
        except Exception as ex:
            print("FATAL", ex)

        try:
            process_sell_statuses(pair)
        except Exception as ex:
            print("FATAL", ex)

        try:
            process_stack_engine(pair)
        except Exception as ex:
            print("FATAL", ex)

conn.close()