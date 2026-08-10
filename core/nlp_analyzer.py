"""NLP 分析模块 —— 关键词提取 + 话题匹配 + 标题优化"""
import json
import re
from typing import List, Tuple

import jieba
import jieba.analyse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import get_config


class NLPAnalyzer:
    """中文文本分析器"""

    # 通用停用词
    STOP_WORDS = set(
        "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 "
        "会 着 没有 看 好 自己 这 他 她 它 们 那 些 什么 而 为 所以 因为 "
        "但是 可以 这个 如果 已经 还 又 只是 虽然 比如 不过 比较 非常 更 "
        "最 很 太 真 挺 特别 比较 那么 怎么 怎样 哪 哪里".split()
    )

    def __init__(self):
        cfg = get_config()
        self.topk = cfg["nlp"]["top_keywords"]
        self.title_max_len = cfg["nlp"]["title_max_length"]

    def extract_keywords(self, text: str) -> List[Tuple[str, float]]:
        """使用 TF-IDF + TextRank 双算法提取关键词"""
        # TF-IDF
        tfidf_kw = jieba.analyse.extract_tags(
            text, topK=self.topk, withWeight=True
        )

        # TextRank
        textrank_kw = jieba.analyse.textrank(
            text, topK=self.topk, withWeight=True
        )

        # 合并去重，取平均权重
        merged = {}
        for word, weight in tfidf_kw:
            if len(word) >= 2 and word not in self.STOP_WORDS:
                merged[word] = max(merged.get(word, 0), weight)

        for word, weight in textrank_kw:
            if len(word) >= 2 and word not in self.STOP_WORDS:
                merged[word] = max(merged.get(word, 0), weight * 1.2)

        return sorted(merged.items(), key=lambda x: x[1], reverse=True)[:self.topk]

    def match_topic(self, keywords: List[str], topics: List[dict]) -> List[Tuple[str, float]]:
        """将关键词匹配到平台话题分类（Jaccard + 余弦相似度）"""
        if not topics:
            return []

        # 构建话题语料
        topic_texts = []
        for t in topics:
            name = t.get("category_name", t.get("name", ""))
            topic_kws = t.get("keywords", [])
            if isinstance(topic_kws, str):
                try:
                    topic_kws = json.loads(topic_kws)
                except json.JSONDecodeError:
                    topic_kws = []

            # 话题名 + 关键词组合
            combined = name + " " + " ".join(topic_kws)
            topic_texts.append(combined)

        # 用 TF-IDF 计算余弦相似度
        article_text = " ".join(keywords)
        vectorizer = TfidfVectorizer(tokenizer=lambda x: x.split(), token_pattern=None, lowercase=False)
        try:
            tfidf_matrix = vectorizer.fit_transform([article_text] + topic_texts)
            similarities = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:]).flatten()
        except ValueError:
            # 词汇不足时退化到 Jaccard
            similarities = []
            kw_set = set(keywords)
            for i, t in enumerate(topics):
                name = t.get("category_name", t.get("name", ""))
                topic_kws = t.get("keywords", [])
                if isinstance(topic_kws, str):
                    try:
                        topic_kws = json.loads(topic_kws)
                    except json.JSONDecodeError:
                        topic_kws = []
                all_topic = set([name] + list(topic_kws))
                jaccard = len(kw_set & all_topic) / len(kw_set | all_topic) if kw_set | all_topic else 0
                similarities.append(jaccard)

        # 按相似度排序
        results = []
        for i, sim in enumerate(similarities):
            name = topics[i].get("category_name", topics[i].get("name", f"topic_{i}"))
            results.append((name, float(sim)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:5]

    def generate_title(self, article_text: str, raw_title: str, platform: str) -> str:
        """为特定平台生成/优化标题"""
        if not raw_title or len(raw_title) < 3:
            # 没有好标题时，用前几个关键词组合
            keywords = self.extract_keywords(article_text)
            if keywords:
                key_words = [kw[0] for kw in keywords[:3]]
                raw_title = "".join(key_words) + "相关文章"

        # 各平台标题长度限制
        # ZOL 创作者中心当前页面限制标题为 5~35 个字。
        limits = {"zol": 35, "xiaoheihe": 30}
        max_len = limits.get(platform, 30)

        # 清理标题
        title = raw_title.strip()

        # 小黑盒风格：更短、更吸引人
        if platform == "xiaoheihe":
            # 移除过于正式的前缀
            title = re.sub(r"^(关于|浅谈|浅析|分析|探讨|研究)\s*[:：]?\s*", "", title)
            if len(title) > max_len:
                title = title[:max_len - 3] + "..."

        # ZOL 风格：保持专业性
        if platform == "zol":
            if len(title) > max_len:
                title = title[:max_len - 3] + "..."

        # 兜底：确保不返回空标题
        if not title:
            title = "无标题文章"
        return title[:max_len]

    def get_keywords_string(self, keywords: List[Tuple[str, float]]) -> str:
        """关键词列表转为 JSON 字符串"""
        return json.dumps(
            [{"word": w, "weight": round(s, 4)} for w, s in keywords],
            ensure_ascii=False,
        )
