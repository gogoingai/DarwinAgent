"""模型注册表契约：端点/密钥/思考参数/缓冲/并发池全部按模型名解析（跟着模型走）。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oak.config import Config
from oak.llm.client import LLMClient
from oak.llm.registry import FALLBACK_PROFILE, request_policy, resolve


class RegistryResolution(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = Config(api_key='k-strong', fast_api_key='k-fast',
                          work_dir=Path(self.td.name))
        self.cfg.fast_base_url = 'https://fast.example/v1'

    def tearDown(self):
        self.td.cleanup()

    def test_longest_prefix_and_site_resolution(self):
        glm = resolve('glm-5.3', self.cfg)
        flash = resolve('glm-5.3-flashx', self.cfg)
        self.assertEqual(glm.profile.thinking, 'offable')
        self.assertEqual((glm.base_url, glm.api_key), (self.cfg.api_base_url, 'k-strong'))
        self.assertEqual(flash.pool_id, glm.pool_id)          # 同站共享池
        self.assertEqual(glm.pool_size, self.cfg.max_concurrency)

        env = {'LOCOMO_FAST_API_KEY': 'k-mm', 'LOCOMO_FAST_API_BASE': 'https://mm.example/v1'}
        with mock.patch.dict(os.environ, env):
            mm = resolve('MiniMax-M3.1-Flash-Preview', self.cfg)
        self.assertEqual((mm.profile.thinking, mm.base_url, mm.api_key, mm.pool_size),
                         ('effort', 'https://mm.example/v1', 'k-mm', 6))

        env = {'COMMANDCODE_API_KEY': 'k-cc', 'COMMANDCODE_BASE_URL': 'https://cc.example/v1'}
        with mock.patch.dict(os.environ, env):
            ds = resolve('deepseek/deepseek-v4.1-flash-fast', self.cfg)
        self.assertEqual((ds.profile.thinking, ds.base_url, ds.api_key, ds.pool_size),
                         ('offable', 'https://cc.example/v1', 'k-cc', 6))
        self.assertNotEqual(ds.pool_id, glm.pool_id)
        self.assertNotEqual(ds.pool_id, mm.pool_id)

    def test_unknown_model_falls_back_conservatively(self):
        r = resolve('totally-unknown-model', self.cfg)
        self.assertIs(r.profile, FALLBACK_PROFILE)
        self.assertEqual(r.base_url, self.cfg.api_base_url)
        # 关不掉的模型：即使角色关思考，也不发参数、缓冲不清零（回归旧档位键控缺陷）
        extra, buf = request_policy(r, thinking_off=True)
        self.assertEqual(extra, {})
        self.assertGreater(buf, 0)


class RequestPolicyMatrix(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.cfg = Config(api_key='k-strong', fast_api_key='k-fast',
                          work_dir=Path(self.td.name))
        self.cfg.fast_base_url = 'https://fast.example/v1'

    def tearDown(self):
        self.td.cleanup()

    def test_offable_glm(self):
        glm = resolve('glm-5.3', self.cfg)
        extra, buf = request_policy(glm, thinking_off=False)
        self.assertEqual(extra, {})
        self.assertEqual(buf, 3072)
        extra, buf = request_policy(glm, thinking_off=True)
        self.assertEqual(extra, {'thinking': {'type': 'disabled'}})
        self.assertEqual(buf, 768)          # 关后残留推理兜底

    def test_effort_minimax(self):
        mm = resolve('MiniMax-M3.1-Flash-Preview', self.cfg)
        extra, buf = request_policy(mm, thinking_off=False, global_effort='')
        self.assertEqual(extra, {'reasoning_effort': 'low'})   # 条目缺省
        self.assertEqual(buf, 32000)
        extra, _ = request_policy(mm, thinking_off=False, global_effort='high')
        self.assertEqual(extra, {'reasoning_effort': 'high'})  # 全局旋钮优先
        extra, _ = request_policy(mm, thinking_off=True, global_effort='low')
        self.assertNotIn('thinking', extra)                    # effort 型无 disabled 可发


    def test_offable_deepseek(self):
        with mock.patch.dict(os.environ, {'COMMANDCODE_API_KEY': 'k-cc'}):
            ds = resolve('deepseek/deepseek-v4-flash-fast', self.cfg)
        extra, buf = request_policy(ds, thinking_off=True)
        self.assertEqual(extra, {'thinking': {'type': 'disabled'}})
        self.assertEqual(buf, 256)
        extra, buf = request_policy(ds, thinking_off=False)
        self.assertEqual(extra, {})
        self.assertEqual(buf, 32000)


class ClientSitePools(unittest.TestCase):
    def test_same_site_shares_pool(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(api_key='k', work_dir=Path(td))
            client = LLMClient(cfg)                # fast 未配置 → 与默认同站
            a = client._site_for('glm-5.3')
            b = client._site_for('glm-5.3-flash')
            self.assertIs(a[0], b[0])
            self.assertIs(a[1], b[1])


if __name__ == '__main__':
    unittest.main()
