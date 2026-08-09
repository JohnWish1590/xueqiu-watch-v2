"""配置加载：优先 config.json，关键密钥可被环境变量覆盖。"""
import json
import os

CONFIG_PATH = os.environ.get('XW_CONFIG', 'config.json')


def load_config():
    p = CONFIG_PATH
    if not os.path.exists(p):
        raise FileNotFoundError(
            f'未找到配置文件: {p}。请复制 config.example.json 为 config.json 并填好。')
    with open(p, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    # 环境变量覆盖敏感字段（避免在配置文件里写明文密钥）
    if os.environ.get('ZECTRIX_API_KEY'):
        cfg.setdefault('zectrix', {})['api_key'] = os.environ['ZECTRIX_API_KEY']
    if os.environ.get('ZECTRIX_DEVICE_MAC'):
        cfg.setdefault('zectrix', {})['device_mac'] = os.environ['ZECTRIX_DEVICE_MAC']
    return cfg


if __name__ == '__main__':
    print(load_config())
