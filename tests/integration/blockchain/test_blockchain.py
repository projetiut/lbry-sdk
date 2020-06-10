import os
import time
import asyncio
import tempfile
import logging
from binascii import hexlify, unhexlify
from random import choice
from distutils.dir_util import copy_tree, remove_tree

from lbry import Config, Database, RegTestLedger, Transaction, Output
from lbry.crypto.base58 import Base58
from lbry.schema.claim import Stream
from lbry.blockchain.lbrycrd import Lbrycrd
from lbry.blockchain.sync import BlockchainSync
from lbry.blockchain.dewies import dewies_to_lbc
from lbry.constants import CENT
from lbry.testcase import AsyncioTestCase


#logging.getLogger('lbry.blockchain').setLevel(logging.DEBUG)


class BlockchainTestCase(AsyncioTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.chain = Lbrycrd.temp_regtest()
        await self.chain.ensure()
        self.addCleanup(self.chain.stop)
        await self.chain.start('-rpcworkqueue=128')


class TestBlockchainEvents(BlockchainTestCase):

    async def test_block_event(self):
        msgs = []

        self.chain.subscribe()
        self.chain.on_block.listen(lambda e: msgs.append(e['msg']))
        res = await self.chain.generate(5)
        await self.chain.on_block.where(lambda e: e['msg'] == 4)
        self.assertEqual([0, 1, 2, 3, 4], msgs)
        self.assertEqual(5, len(res))

        self.chain.unsubscribe()
        res = await self.chain.generate(2)
        self.assertEqual(2, len(res))
        await asyncio.sleep(0.1)  # give some time to "miss" the new block events

        self.chain.subscribe()
        res = await self.chain.generate(3)
        await self.chain.on_block.where(lambda e: e['msg'] == 9)
        self.assertEqual(3, len(res))
        self.assertEqual([
            0, 1, 2, 3, 4,
            # 5, 6 "missed"
            7, 8, 9
        ], msgs)


class TestMultiBlockFileSyncAndEvents(AsyncioTestCase):

    TEST_DATA_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'tmp-lbry-sync-test-data')
    LBRYCRD_ARGS = '-maxblockfilesize=8', '-rpcworkqueue=128'

    async def asyncSetUp(self):
        await super().asyncSetUp()

        generate = True
        if os.path.exists(self.TEST_DATA_CACHE_DIR):
            generate = False
            temp_dir = tempfile.mkdtemp()
            copy_tree(self.TEST_DATA_CACHE_DIR, temp_dir)
            self.chain = Lbrycrd(RegTestLedger(Config.with_same_dir(temp_dir)))
        else:
            self.chain = Lbrycrd.temp_regtest()

        await self.chain.ensure()
        await self.chain.start(*self.LBRYCRD_ARGS)
        self.addCleanup(self.chain.stop)

        self.db = Database.temp_sqlite_regtest(self.chain.ledger.conf.lbrycrd_dir)
        self.addCleanup(remove_tree, self.db.ledger.conf.data_dir)
        await self.db.open()
        self.addCleanup(self.db.close)
        self.chain.ledger.conf.spv_address_filters = False
        self.sync = BlockchainSync(self.chain, self.db)

        if not generate:
            return

        print(f'generating sample claims... ', end='', flush=True)

        await self.chain.generate(101)

        names = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']
        address = Base58.decode(await self.chain.get_new_address())

        start = time.perf_counter()
        for _ in range(190):
            tx = Transaction().add_outputs([
                Output.pay_claim_name_pubkey_hash(
                    CENT, f'{choice(names)}{i}',
                    Stream().update(
                        title='a claim title',
                        description='Lorem ipsum '*400,
                        tag=['crypto', 'health', 'space'],
                    ).claim,
                    address)
                for i in range(1, 20)
            ])
            funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
            signed = await self.chain.sign_raw_transaction_with_wallet(funded['hex'])
            await self.chain.send_raw_transaction(signed['hex'])
            await self.chain.generate(1)

        print(f'took {time.perf_counter()-start}s to generate {190*19} claims ', flush=True)

        await self.chain.stop(False)
        copy_tree(self.chain.ledger.conf.lbrycrd_dir, self.TEST_DATA_CACHE_DIR)
        await self.chain.start(*self.LBRYCRD_ARGS)

    @staticmethod
    def extract_events(name, events):
        return sorted([
            [p['data'].get('block_file'), p['data']['step'], p['data']['total']]
            for p in events if p['event'].endswith(name)
        ])

    def assertEventsAlmostEqual(self, actual, expected):
        # this is needed because the sample tx data created
        # by lbrycrd does not have deterministic number of TXIs,
        # which throws off the progress reporting steps
        # adjust the actual to match expected if it's only off by 1
        for e, a in zip(expected, actual):
            if a[1] != e[1] and abs(a[1]-e[1]) <= 1:
                a[1] = e[1]
        self.assertEqual(expected, actual)

    async def test_multi_block_file_sync(self):
        self.assertEqual(
            [(0, 191, 280), (1, 89, 178), (2, 12, 24)],
            [(file['file_number'], file['blocks'], file['txs'])
             for file in await self.chain.db.get_block_files()]
        )
        self.assertEqual(191, len(await self.chain.db.get_blocks_in_file(0)))

        events = []
        self.sync.on_progress.listen(events.append)

        await self.sync.advance()
        self.assertEqual(
            events[0], {
                "event": "blockchain.sync.start",
                "data": {
                    "starting_height": -1,
                    "ending_height": 291,
                    "files": 3,
                    "blocks": 292,
                    "txs": 482
                }
            }
        )
        self.assertEqual(
            self.extract_events('block.read', events), [
                [0, 0, 191],
                [0, 100, 191],
                [0, 191, 191],
                [1, 0, 89],
                [1, 89, 89],
                [2, 0, 12],
                [2, 12, 12],
            ]
        )
        self.assertEventsAlmostEqual(
            self.extract_events('block.save', events), [
                [0, 0, 280],
                [0, 19, 280],
                [0, 47, 280],
                [0, 267, 280],
                [0, 278, 280],
                [0, 280, 280],
                [1, 0, 178],
                [1, 6, 178],
                [1, 19, 178],
                [1, 166, 178],
                [1, 175, 178],
                [1, 178, 178],
                [2, 0, 24],
                [2, 1, 24],
                [2, 21, 24],
                [2, 22, 24],
                [2, 24, 24]
            ]
        )
        claim_events = self.extract_events('claim.update', events)
        self.assertEqual([3402, 3610], claim_events[2][1:])
        self.assertEqual([3610, 3610], claim_events[-1][1:])

        events.clear()
        await self.sync.advance()  # should be no-op
        self.assertListEqual([], events)

        await self.chain.generate(1)

        events.clear()

        await self.sync.advance()
        self.assertEqual(
            events[0], {
                "event": "blockchain.sync.start",
                "data": {
                    "starting_height": 291,
                    "ending_height": 292,
                    "files": 1,
                    "blocks": 1,
                    "txs": 1
                }
            }
        )
        self.assertEqual(
            self.extract_events('block.read', events), [
                [2, 0, 1],
                [2, 1, 1],
            ]
        )
        self.assertEqual(
            self.extract_events('block.save', events), [
                [2, 0, 1],
                [2, 1, 1],
            ]
        )


class BaseSyncTestCase(BlockchainTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()

        self.current_height = 0
        await self.generate(101, wait=False)

        self.db = Database.temp_sqlite_regtest(self.chain.ledger.conf.lbrycrd_dir)
        self.addCleanup(remove_tree, self.db.ledger.conf.data_dir)
        await self.db.open()
        self.addCleanup(self.db.close)

        self.chain.ledger.conf.spv_address_filters = False
        self.sync = BlockchainSync(self.chain, self.db)
        await self.sync.start()
        self.addCleanup(self.sync.stop)

        self.last_block_hash = None
        self.address = await self.chain.get_new_address()

    async def generate(self, blocks, wait=True):
        block_hashes = await self.chain.generate(blocks)
        self.current_height += blocks
        self.last_block_hash = block_hashes[-1]
        if wait:
            await self.sync.on_block.where(lambda b: self.current_height == b.height)
        return block_hashes

    async def get_transaction(self, txid):
        raw = await self.chain.get_raw_transaction(txid)
        return Transaction(unhexlify(raw))

    async def get_last_block(self):
        return await self.chain.get_block(self.last_block_hash)

    def find_claim_txo(self, tx):
        for txo in tx.outputs:
            if txo.is_claim:
                return txo

    async def claim_name(self, title, amount):
        claim = Stream().update(title=title).claim
        return await self.chain.claim_name(
            'foo', hexlify(claim.to_bytes()).decode(), amount
        )

    async def claim_update(self, tx, amount):
        claim = self.find_claim_txo(tx).claim
        return await self.chain.update_claim(
            tx.outputs[0].tx_ref.id, hexlify(claim.to_bytes()).decode(), amount
        )

    async def claim_abandon(self, tx):
        return await self.chain.abandon_claim(tx.id, self.address)

    async def support_claim(self, tx, amount):
        txo = self.find_claim_txo(tx)
        response = await self.chain.support_claim(
            txo.claim_name, txo.claim_id, amount
        )
        return response['txId']


class TestBasicSyncScenarios(BaseSyncTestCase):

    async def test_sync_advances(self):
        blocks = []
        self.sync.on_block.listen(blocks.append)
        await self.generate(1)
        await self.generate(1)
        await self.generate(1)
        self.assertEqual([102, 103, 104], [b.height for b in blocks])
        self.assertEqual(104, self.current_height)
        blocks.clear()
        await self.generate(6)
        self.assertEqual([110], [b.height for b in blocks])
        self.assertEqual(110, self.current_height)

    async def test_claim_create_update_and_delete(self):
        txid = await self.claim_name('foo', '0.01')
        await self.generate(1)
        claims = await self.db.search_claims()
        self.assertEqual(1, len(claims))
        self.assertEqual(claims[0].claim_name, 'foo')
        self.assertEqual(dewies_to_lbc(claims[0].amount), '0.01')
        txid = await self.claim_update(await self.get_transaction(txid), '0.02')
        await self.generate(1)
        claims = await self.db.search_claims()
        self.assertEqual(1, len(claims))
        self.assertEqual(claims[0].claim_name, 'foo')
        self.assertEqual(dewies_to_lbc(claims[0].amount), '0.02')
        await self.claim_abandon(await self.get_transaction(txid))
        await self.generate(1)
        claims = await self.db.search_claims()
        self.assertEqual(0, len(claims))


class TestClaimtrieSync(BaseSyncTestCase):

    async def advance(self, new_height, ops):
        blocks = (new_height-self.current_height)-1
        if blocks > 0:
            await self.generate(blocks)
        txs = []
        for op in ops:
            if len(op) == 3:
                op_type, value, amount = op
            else:
                (op_type, value), amount = op, None
            if op_type == 'claim':
                txid = await self.claim_name(value, amount)
            elif op_type == 'update':
                txid = await self.claim_update(value, amount)
            elif op_type == 'abandon':
                txid = await self.claim_abandon(value)
            elif op_type == 'support':
                txid = await self.support_claim(value, amount)
            else:
                raise ValueError(f'"{op_type}" is unknown operation')
            txs.append(await self.get_transaction(txid))
        await self.generate(1)
        return txs

    async def get_controlling(self):
        sql = f"""
            select
                tx.height, tx.raw, txo.position, effective_amount, activation_height
            from claimtrie
                join claim using (claim_hash)
                join txo using (txo_hash)
                join tx using (tx_hash)
            where
                txo.txo_type in (1, 2) and
                expiration_height > {self.current_height}
        """
        for claim in await self.db.execute_fetchall(sql):
            tx = Transaction(claim['raw'], height=claim['height'])
            txo = tx.outputs[claim['position']]
            return (
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(claim['effective_amount']), claim['activation_height']
            )

    async def get_active(self):
        controlling = await self.get_controlling()
        active = []
        sql = f"""
        select tx.height, tx.raw, txo.position, effective_amount, activation_height
        from txo
            join tx using (tx_hash)
            join claim using (claim_hash)
        where
            txo.txo_type in (1, 2) and
            activation_height <= {self.current_height} and
            expiration_height > {self.current_height}
        """
        for claim in await self.db.execute_fetchall(sql):
            tx = Transaction(claim['raw'], height=claim['height'])
            txo = tx.outputs[claim['position']]
            if controlling and controlling[0] == txo.claim.stream.title:
                continue
            active.append((
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(claim['effective_amount']), claim['activation_height']
            ))
        return active

    async def get_accepted(self):
        accepted = []
        sql = f"""
        select tx.height, tx.raw, txo.position, effective_amount, activation_height
        from txo
            join tx using (tx_hash)
            join claim using (claim_hash)
        where
            txo.txo_type in (1, 2) and
            activation_height > {self.current_height} and
            expiration_height > {self.current_height}
        """
        for claim in await self.db.execute_fetchall(sql):
            tx = Transaction(claim['raw'], height=claim['height'])
            txo = tx.outputs[claim['position']]
            accepted.append((
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(claim['effective_amount']), claim['activation_height']
            ))
        return accepted

    async def state(self, controlling=None, active=None, accepted=None):
        self.assertEqual(controlling, await self.get_controlling())
        self.assertEqual(active or [], await self.get_active())
        self.assertEqual(accepted or [], await self.get_accepted())

    async def test_example_from_spec(self):
        # https://spec.lbry.com/#claim-activation-example
        advance, state = self.advance, self.state
        stream, = await advance(113, [('claim', 'Claim A', '10.0')])
        await state(
            controlling=('Claim A', '10.0', '10.0', 113),
            active=[],
            accepted=[]
        )
        await advance(501, [('claim', 'Claim B', '20.0')])
        await state(
            controlling=('Claim A', '10.0', '10.0', 113),
            active=[],
            accepted=[('Claim B', '20.0', '0.0', 513)]
        )
        await advance(510, [('support', stream, '14')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[],
            accepted=[('Claim B', '20.0', '0.0', 513)]
        )
        await advance(512, [('claim', 'Claim C', '50.0')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[],
            accepted=[
                ('Claim B', '20.0', '0.0', 513),
                ('Claim C', '50.0', '0.0', 524)]
        )
        await advance(513, [])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[('Claim B', '20.0', '20.0', 513)],
            accepted=[('Claim C', '50.0', '0.0', 524)]
        )
        await advance(520, [('claim', 'Claim D', '60.0')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[('Claim B', '20.0', '20.0', 513)],
            accepted=[
                ('Claim C', '50.0', '0.0', 524),
                ('Claim D', '60.0', '0.0', 532)]
        )
        await advance(524, [])
        await state(
            controlling=('Claim D', '60.0', '60.0', 524),
            active=[
                ('Claim A', '10.0', '24.0', 113),
                ('Claim B', '20.0', '20.0', 513),
                ('Claim C', '50.0', '50.0', 524)],
            accepted=[]
        )
        # beyond example
        await advance(525, [('update', stream, '70.0')])
        await state(
            controlling=('Claim A', '70.0', '84.0', 525),
            active=[
                ('Claim B', '20.0', '20.0', 513),
                ('Claim C', '50.0', '50.0', 524),
                ('Claim D', '60.0', '60.0', 524),
            ],
            accepted=[]
        )

    async def test_competing_claims_subsequent_blocks_height_wins(self):
        advance, state = self.advance, self.state
        await advance(113, [('claim', 'Claim A', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[],
            accepted=[]
        )
        await advance(114, [('claim', 'Claim B', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[('Claim B', '1.0', '1.0', 114)],
            accepted=[]
        )
        await advance(115, [('claim', 'Claim C', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[
                ('Claim B', '1.0', '1.0', 114),
                ('Claim C', '1.0', '1.0', 115)],
            accepted=[]
        )

    async def test_competing_claims_in_single_block_position_wins(self):
        claim_a, claim_b = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '1.0')
        ])
        block = await self.get_last_block()
        # order of tx in block is non-deterministic,
        # figure out what ordered we ended up with
        if block['tx'][1] == claim_a.id:
            winner, other = 'Claim A', 'Claim B'
        else:
            winner, other = 'Claim B', 'Claim A'
        await self.state(
            controlling=(winner, '1.0', '1.0', 113),
            active=[(other, '1.0', '1.0', 113)],
            accepted=[]
        )

    async def test_competing_claims_in_single_block_effective_amount_wins(self):
        await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )

    async def test_winning_claim_deleted(self):
        claim1, claim2 = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )
        await self.advance(114, [('abandon', claim2)])
        await self.state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[],
            accepted=[]
        )

    async def test_winning_claim_deleted_and_new_claim_becomes_winner(self):
        claim1, claim2 = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )
        await self.advance(115, [
            ('abandon', claim2),
            ('claim', 'Claim C', '3.0')
        ])
        await self.state(
            controlling=('Claim C', '3.0', '3.0', 115),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )

    async def test_winning_claim_expires_and_another_takes_over(self):
        await self.advance(110, [('claim', 'Claim A', '2.0')])
        await self.advance(120, [('claim', 'Claim B', '1.0')])
        await self.state(
            controlling=('Claim A', '2.0', '2.0', 110),
            active=[('Claim B', '1.0', '1.0', 120)],
            accepted=[]
        )
        await self.advance(610, [])
        await self.state(
            controlling=('Claim B', '1.0', '1.0', 120),
            active=[],
            accepted=[]
        )
        await self.advance(620, [])
        await self.state(
            controlling=None,
            active=[],
            accepted=[]
        )

    async def test_create_and_multiple_updates_in_same_block(self):
        await self.chain.generate(10)
        txid = await self.claim_name('Claim A', '1.0')
        txid = await self.claim_update(await self.get_transaction(txid), '2.0')
        await self.claim_update(await self.get_transaction(txid), '3.0')
        await self.chain.generate(1)
        await self.sync.advance()
        self.current_height += 11
        await self.state(
            controlling=('Claim A', '3.0', '3.0', 112),
            active=[],
            accepted=[]
        )

    async def test_create_and_abandon_in_same_block(self):
        await self.chain.generate(10)
        txid = await self.claim_name('Claim A', '1.0')
        await self.claim_abandon(await self.get_transaction(txid))
        await self.chain.generate(1)
        await self.sync.advance()
        self.current_height += 11
        await self.state(
            controlling=None,
            active=[],
            accepted=[]
        )
