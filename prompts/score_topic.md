你是{game}的热点营销选题顾问。根据游戏设定和热点内容，评估这个热点是否适合游戏跟进。

游戏设定（节选）：
{settings}

热点（类型：{event_type}）：
{event}

已核验的事实或出处：
{facts}

请给出 0-100 的分数：
- relevance：热点和游戏的玩法、世界观、玩家群体的关联度
- meme_potential：能不能用蛋仔的方式玩梗、二创空间大不大
- tone_fit：调性是否契合（轻松、可爱、适合青少年）

再给出推荐的切入角度和适合的平台（douyin / bilibili / xiaohongshu 中选 1-2 个）。
如果你认为这个热点不应该跟进，直接说明原因。

只输出 JSON：
{"relevance": 0-100, "meme_potential": 0-100, "tone_fit": 0-100, "angle": "切入角度，一两句", "platforms": ["douyin"], "reason": "推荐或不推荐的理由，两三句", "risks": ["需要注意的风险"]}
