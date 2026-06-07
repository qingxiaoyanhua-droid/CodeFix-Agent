"""
Enterprise-Grade RAG Pipeline for Code Repair
Production-ready implementation with:
- Web scraping and data collection
- Deduplication and noise reduction
- Advanced chunking strategies
- Hybrid search (BM25 + Vector + Keyword)
- Multi-stage reranking
- Quality assurance pipeline
"""

import os
import re
import json
import hashlib
import requests
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, asdict
import pickle
import numpy as np
from collections import Counter
import math
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ----------------------------
# Data Models
# ----------------------------

@dataclass
class CodeSnippet:
    """代码片段数据模型"""
    id: str
    source: str  # GitHub URL or file path
    language: str
    code: str
    description: str
    tags: List[str]
    bug_type: Optional[str] = None
    fix_patch: Optional[str] = None
    metadata: Dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        self.id = self.id or hashlib.md5(self.code.encode()).hexdigest()


@dataclass
class Chunk:
    """文本块数据模型"""
    id: str
    content: str
    metadata: Dict
    embeddings: Optional[np.ndarray] = None
    bm25_score: float = 0.0
    vector_score: float = 0.0
    final_score: float = 0.0


@dataclass
class SearchResult:
    """搜索结果数据模型"""
    chunk: Chunk
    score: float
    source: str
    relevance: float


# ----------------------------
# Data Collection Module
# ----------------------------

class CodeDataCollector:
    """代码数据收集器 - 从多个源收集代码修复数据"""
    
    def __init__(self, output_dir: str = "./data/raw"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; CodeRepairBot/1.0)'
        })
    
    def collect_from_github(self, query: str, max_results: int = 1000) -> List[CodeSnippet]:
        """从 GitHub 收集代码修复数据"""
        logger.info(f"Collecting from GitHub with query: {query}")
        snippets = []
        
        # GitHub Search API
        url = "https://api.github.com/search/code"
        params = {
            'q': query,
            'per_page': 100,
            'page': 1
        }
        
        headers = {
            'Accept': 'application/vnd.github.v3+json'
        }
        
        # Note: Requires GitHub token for production use
        github_token = os.getenv('GITHUB_TOKEN', '')
        if github_token:
            headers['Authorization'] = f'token {github_token}'
        
        while len(snippets) < max_results:
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                
                items = data.get('items', [])
                for item in items:
                    snippet = self._parse_github_item(item)
                    if snippet:
                        snippets.append(snippet)
                
                # Check if there are more pages
                if 'next' not in response.links:
                    break
                params['page'] += 1
                
                # Rate limiting
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error collecting from GitHub: {e}")
                break
        
        logger.info(f"Collected {len(snippets)} snippets from GitHub")
        return snippets
    
    def collect_from_stackoverflow(self, tags: List[str], max_results: int = 500) -> List[CodeSnippet]:
        """从 Stack Overflow 收集代码修复数据"""
        logger.info(f"Collecting from Stack Overflow with tags: {tags}")
        snippets = []
        
        url = "https://api.stackexchange.com/2.3/questions"
        params = {
            'tagged': ';'.join(tags),
            'site': 'stackoverflow',
            'pagesize': 100,
            'page': 1,
            'order': 'desc',
            'sort': 'votes'
        }
        
        while len(snippets) < max_results:
            try:
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()
                
                items = data.get('items', [])
                for item in items:
                    snippet = self._parse_stackoverflow_item(item)
                    if snippet:
                        snippets.append(snippet)
                
                if not data.get('has_more', False):
                    break
                params['page'] += 1
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error collecting from Stack Overflow: {e}")
                break
        
        logger.info(f"Collected {len(snippets)} snippets from Stack Overflow")
        return snippets
    
    def collect_from_local_repos(self, repo_paths: List[str]) -> List[CodeSnippet]:
        """从本地 Git 仓库收集代码修复数据"""
        logger.info(f"Collecting from local repositories: {repo_paths}")
        snippets = []
        
        for repo_path in repo_paths:
            try:
                repo_snippets = self._collect_from_git_log(repo_path)
                snippets.extend(repo_snippets)
                logger.info(f"Collected {len(repo_snippets)} snippets from {repo_path}")
            except Exception as e:
                logger.error(f"Error collecting from {repo_path}: {e}")
        
        return snippets
    
    def _parse_github_item(self, item: Dict) -> Optional[CodeSnippet]:
        """解析 GitHub 搜索结果"""
        try:
            # Fetch file content
            download_url = item.get('git_url', '')
            if not download_url:
                return None
            
            response = self.session.get(download_url, timeout=30)
            response.raise_for_status()
            content = response.json().get('content', '')
            
            # Decode base64 if needed
            import base64
            try:
                content = base64.b64decode(content).decode('utf-8')
            except:
                pass
            
            return CodeSnippet(
                id=hashlib.md5(content.encode()).hexdigest(),
                source=item.get('html_url', ''),
                language=item.get('language', 'unknown'),
                code=content,
                description=f"File: {item.get('name', '')}",
                tags=[item.get('language', 'unknown')],
                metadata={
                    'repository': item.get('repository', {}).get('full_name', ''),
                    'path': item.get('path', '')
                }
            )
        except Exception as e:
            logger.error(f"Error parsing GitHub item: {e}")
            return None
    
    def _parse_stackoverflow_item(self, item: Dict) -> Optional[CodeSnippet]:
        """解析 Stack Overflow 问题"""
        try:
            # Extract code blocks from question and answers
            code_blocks = re.findall(r'<pre><code>(.*?)</code></pre>', 
                                    item.get('body', ''), re.DOTALL)
            
            if not code_blocks:
                return None
            
            code = '\n'.join(code_blocks)
            
            return CodeSnippet(
                id=hashlib.md5(code.encode()).hexdigest(),
                source=f"https://stackoverflow.com/questions/{item.get('question_id')}",
                language='mixed',
                code=code,
                description=item.get('title', ''),
                tags=item.get('tags', []),
                metadata={
                    'score': item.get('score', 0),
                    'view_count': item.get('view_count', 0),
                    'answer_count': item.get('answer_count', 0)
                }
            )
        except Exception as e:
            logger.error(f"Error parsing Stack Overflow item: {e}")
            return None
    
    def _collect_from_git_log(self, repo_path: str) -> List[CodeSnippet]:
        """从 Git 提交历史中收集代码修复"""
        snippets = []
        
        # Get commit history with bug fix keywords
        keywords = ['fix', 'bug', 'repair', 'patch', 'resolve', 'correct']
        
        for keyword in keywords:
            try:
                result = subprocess.run(
                    ['git', 'log', '--oneline', '--grep', keyword, '--all'],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                
                commits = result.stdout.strip().split('\n')
                for commit in commits[:50]:  # Limit per keyword
                    if commit:
                        commit_hash = commit.split()[0]
                        snippet = self._extract_commit_changes(repo_path, commit_hash)
                        if snippet:
                            snippets.append(snippet)
            except Exception as e:
                logger.error(f"Error collecting from git log: {e}")
        
        return snippets
    
    def _extract_commit_changes(self, repo_path: str, commit_hash: str) -> Optional[CodeSnippet]:
        """从 commit 中提取代码变更"""
        try:
            # Get commit diff
            result = subprocess.run(
                ['git', 'show', commit_hash, '--no-merges'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            diff = result.stdout
            
            # Extract code changes
            code_changes = re.findall(r'\+[^+].*?(?=\n)', diff)
            if not code_changes:
                return None
            
            return CodeSnippet(
                id=hashlib.md5(diff.encode()).hexdigest(),
                source=f"{repo_path}/commit/{commit_hash}",
                language='diff',
                code=diff,
                description=f"Commit: {commit_hash}",
                tags=['bugfix', 'commit'],
                metadata={
                    'commit_hash': commit_hash,
                    'repo_path': repo_path
                }
            )
        except Exception as e:
            logger.error(f"Error extracting commit changes: {e}")
            return None
    
    def save(self, snippets: List[CodeSnippet], filename: str = "collected_data.json"):
        """保存收集的数据"""
        output_path = self.output_dir / filename
        data = [asdict(s) for s in snippets]
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(snippets)} snippets to {output_path}")
    
    def load(self, filename: str = "collected_data.json") -> List[CodeSnippet]:
        """加载已保存的数据"""
        input_path = self.output_dir / filename
        if not input_path.exists():
            return []
        
        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        return [CodeSnippet(**item) for item in data]


# ----------------------------
# Data Cleaning Module
# ----------------------------

class DataCleaner:
    """数据清洗器 - 去重、降噪、质量过滤"""
    
    def __init__(self):
        self.seen_hashes = set()
    
    def deduplicate(self, snippets: List[CodeSnippet], method: str = 'exact') -> List[CodeSnippet]:
        """去重"""
        logger.info(f"Deduplicating {len(snippets)} snippets using {method} method")
        unique_snippets = []
        
        for snippet in snippets:
            if method == 'exact':
                hash_value = hashlib.md5(snippet.code.encode()).hexdigest()
            elif method == 'normalized':
                # Normalize code (remove whitespace, comments, etc.)
                normalized = self._normalize_code(snippet.code)
                hash_value = hashlib.md5(normalized.encode()).hexdigest()
            else:
                hash_value = hashlib.md5(snippet.code.encode()).hexdigest()
            
            if hash_value not in self.seen_hashes:
                self.seen_hashes.add(hash_value)
                unique_snippets.append(snippet)
        
        logger.info(f"Deduplicated to {len(unique_snippets)} unique snippets")
        return unique_snippets
    
    def remove_noise(self, snippets: List[CodeSnippet]) -> List[CodeSnippet]:
        """降噪 - 移除低质量代码"""
        logger.info(f"Removing noise from {len(snippets)} snippets")
        clean_snippets = []
        
        for snippet in snippets:
            if self._is_high_quality(snippet):
                clean_snippets.append(snippet)
        
        logger.info(f"Cleaned to {len(clean_snippets)} high-quality snippets")
        return clean_snippets
    
    def filter_by_quality(self, snippets: List[CodeSnippet], 
                         min_length: int = 10, 
                         max_length: int = 10000) -> List[CodeSnippet]:
        """按质量过滤"""
        logger.info(f"Filtering by quality: min_length={min_length}, max_length={max_length}")
        filtered = []
        
        for snippet in snippets:
            code_length = len(snippet.code)
            if min_length <= code_length <= max_length:
                # Additional quality checks
                if self._has_meaningful_content(snippet):
                    filtered.append(snippet)
        
        logger.info(f"Filtered to {len(filtered)} quality snippets")
        return filtered
    
    def _normalize_code(self, code: str) -> str:
        """标准化代码（用于去重）"""
        # Remove comments
        code = re.sub(r'//.*?\n|/\*.*?\*/|#.*?\n', '', code, flags=re.DOTALL)
        # Remove whitespace
        code = re.sub(r'\s+', ' ', code)
        # Remove string literals
        code = re.sub(r'["\'].*?["\']', '""', code)
        return code.strip().lower()
    
    def _is_high_quality(self, snippet: CodeSnippet) -> bool:
        """检查代码质量"""
        code = snippet.code
        
        # Check for minimum length
        if len(code) < 10:
            return False
        
        # Check for too many special characters
        special_char_ratio = len(re.findall(r'[^a-zA-Z0-9\s]', code)) / len(code)
        if special_char_ratio > 0.5:
            return False
        
        # Check for meaningful content (has functions, classes, etc.)
        has_structure = bool(re.search(r'(def |class |function |public |private )', code))
        
        # Check for boilerplate or placeholder code
        has_placeholder = bool(re.search(r'(lorem ipsum|placeholder|todo|fixme)', code, re.I))
        
        return has_structure and not has_placeholder
    
    def _has_meaningful_content(self, snippet: CodeSnippet) -> bool:
        """检查是否有意义的内容"""
        code = snippet.code
        
        # Check line count
        lines = code.split('\n')
        if len(lines) < 3:
            return False
        
        # Check for code-to-comment ratio
        comment_lines = sum(1 for line in lines if line.strip().startswith(('#', '//', '/*', '*')))
        comment_ratio = comment_lines / len(lines)
        
        # Too many comments might indicate documentation rather than code
        if comment_ratio > 0.8:
            return False
        
        return True


# ----------------------------
# Chunking Module
# ----------------------------

class ChunkingStrategy:
    """文本分块策略"""
    
    def __init__(self, method: str = 'semantic', chunk_size: int = 512, overlap: int = 50):
        self.method = method
        self.chunk_size = chunk_size
        self.overlap = overlap
    
    def chunk(self, text: str, metadata: Dict = None) -> List[Chunk]:
        """将文本分块"""
        if self.method == 'fixed':
            return self._fixed_size_chunking(text, metadata)
        elif self.method == 'semantic':
            return self._semantic_chunking(text, metadata)
        elif self.method == 'function':
            return self._function_based_chunking(text, metadata)
        else:
            return self._fixed_size_chunking(text, metadata)
    
    def _fixed_size_chunking(self, text: str, metadata: Dict = None) -> List[Chunk]:
        """固定大小分块"""
        chunks = []
        tokens = text.split()
        
        for i in range(0, len(tokens), self.chunk_size - self.overlap):
            chunk_tokens = tokens[i:i + self.chunk_size]
            chunk_text = ' '.join(chunk_tokens)
            
            chunk = Chunk(
                id=hashlib.md5(chunk_text.encode()).hexdigest(),
                content=chunk_text,
                metadata=metadata or {}
            )
            chunks.append(chunk)
        
        return chunks
    
    def _semantic_chunking(self, text: str, metadata: Dict = None) -> List[Chunk]:
        """语义分块（基于代码结构）"""
        chunks = []
        
        # Split by function/class definitions
        pattern = r'(?=(?:def |class |function |public |private ))'
        segments = re.split(pattern, text)
        
        current_chunk = ""
        for segment in segments:
            if len(current_chunk) + len(segment) <= self.chunk_size:
                current_chunk += segment
            else:
                if current_chunk:
                    chunk = Chunk(
                        id=hashlib.md5(current_chunk.encode()).hexdigest(),
                        content=current_chunk,
                        metadata=metadata or {}
                    )
                    chunks.append(chunk)
                current_chunk = segment
        
        if current_chunk:
            chunk = Chunk(
                id=hashlib.md5(current_chunk.encode()).hexdigest(),
                content=current_chunk,
                metadata=metadata or {}
            )
            chunks.append(chunk)
        
        return chunks
    
    def _function_based_chunking(self, text: str, metadata: Dict = None) -> List[Chunk]:
        """基于函数的分块（每个函数一个 chunk）"""
        chunks = []
        
        # Find function definitions
        pattern = r'(?:def |class |function ).*?(?=\n(?:def |class |function )|\Z)'
        matches = re.finditer(pattern, text, re.DOTALL)
        
        for match in matches:
            func_text = match.group()
            chunk = Chunk(
                id=hashlib.md5(func_text.encode()).hexdigest(),
                content=func_text,
                metadata=metadata or {}
            )
            chunks.append(chunk)
        
        return chunks


# ----------------------------
# Embedding Module
# ----------------------------

class EmbeddingModel:
    """嵌入模型接口"""
    
    def __init__(self, model_name: str = 'sentence-transformers/all-MiniLM-L6-v2'):
        self.model_name = model_name
        self.model = None
        self._load_model()
    
    def _load_model(self):
        """加载嵌入模型"""
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(self.model_name)
            logger.info(f"Loaded embedding model: {self.model_name}")
        except ImportError:
            logger.warning("sentence-transformers not installed. Using dummy embeddings.")
            self.model = None
    
    def encode(self, texts: List[str], batch_size: int = 32) -> np.ndarray:
        """编码文本为向量"""
        if self.model is None:
            # Return dummy embeddings
            return np.random.randn(len(texts), 384)
        
        embeddings = self.model.encode(
            texts, 
            batch_size=batch_size, 
            show_progress_bar=True,
            convert_to_numpy=True
        )
        return embeddings
    
    def encode_query(self, query: str) -> np.ndarray:
        """编码查询"""
        if self.model is None:
            return np.random.randn(384)
        return self.model.encode([query], convert_to_numpy=True)[0]


# ----------------------------
# BM25 Module
# ----------------------------

class BM25Index:
    """BM25 索引"""
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents = []
        self.avg_doc_length = 0
        self.doc_lengths = []
        self.term_freq = {}
        self.doc_freq = {}
        self.num_docs = 0
    
    def index(self, documents: List[str]):
        """构建索引"""
        logger.info(f"Building BM25 index with {len(documents)} documents")
        self.documents = documents
        self.num_docs = len(documents)
        
        # Tokenize and compute statistics
        self.doc_lengths = []
        self.term_freq = {}
        self.doc_freq = {}
        
        for doc_id, doc in enumerate(documents):
            tokens = self._tokenize(doc)
            doc_length = len(tokens)
            self.doc_lengths.append(doc_length)
            
            # Term frequency
            tf = Counter(tokens)
            self.term_freq[doc_id] = tf
            
            # Document frequency
            for term in set(tokens):
                if term not in self.doc_freq:
                    self.doc_freq[term] = 0
                self.doc_freq[term] += 1
        
        self.avg_doc_length = sum(self.doc_lengths) / self.num_docs if self.num_docs > 0 else 0
        logger.info(f"BM25 index built. Avg doc length: {self.avg_doc_length:.2f}")
    
    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        """搜索"""
        query_tokens = self._tokenize(query)
        scores = {}
        
        for doc_id in range(self.num_docs):
            score = 0
            doc_length = self.doc_lengths[doc_id]
            
            for term in query_tokens:
                if term in self.doc_freq:
                    # IDF
                    idf = math.log((self.num_docs - self.doc_freq[term] + 0.5) / 
                                  (self.doc_freq[term] + 0.5) + 1)
                    
                    # TF
                    tf = self.term_freq[doc_id].get(term, 0)
                    
                    # BM25 score
                    numerator = tf * (self.k1 + 1)
                    denominator = tf + self.k1 * (1 - self.b + self.b * doc_length / self.avg_doc_length)
                    
                    score += idf * numerator / denominator
            
            if score > 0:
                scores[doc_id] = score
        
        # Sort by score
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:top_k]
    
    def _tokenize(self, text: str) -> List[str]:
        """分词"""
        # Simple tokenization
        tokens = re.findall(r'\b\w+\b', text.lower())
        return tokens


# ----------------------------
# Hybrid Search Module
# ----------------------------

class HybridSearch:
    """混合检索系统（BM25 + 向量检索 + 关键词）"""
    
    def __init__(self, embedding_model: EmbeddingModel = None):
        self.embedding_model = embedding_model or EmbeddingModel()
        self.bm25_index = BM25Index()
        self.chunks = []
        self.chunk_embeddings = None
    
    def index(self, chunks: List[Chunk]):
        """构建索引"""
        logger.info(f"Indexing {len(chunks)} chunks")
        self.chunks = chunks
        
        # Build BM25 index
        documents = [chunk.content for chunk in chunks]
        self.bm25_index.index(documents)
        
        # Compute embeddings
        logger.info("Computing embeddings...")
        self.chunk_embeddings = self.embedding_model.encode(documents)
        logger.info(f"Indexed {len(chunks)} chunks with embeddings shape: {self.chunk_embeddings.shape}")
    
    def search(self, query: str, top_k: int = 10, 
               bm25_weight: float = 0.4, 
               vector_weight: float = 0.6) -> List[SearchResult]:
        """混合检索"""
        logger.info(f"Searching for query: {query[:50]}...")
        
        # BM25 search
        bm25_results = self.bm25_index.search(query, top_k=top_k * 2)
        bm25_scores = {doc_id: score for doc_id, score in bm25_results}
        
        # Vector search
        query_embedding = self.embedding_model.encode_query(query)
        vector_scores = self._cosine_similarity(query_embedding, self.chunk_embeddings)
        
        # Normalize scores
        bm25_scores_normalized = self._normalize_scores(bm25_scores)
        vector_scores_normalized = self._normalize_scores(
            {i: score for i, score in enumerate(vector_scores)}
        )
        
        # Combine scores
        combined_scores = {}
        for i in range(len(self.chunks)):
            bm25_score = bm25_scores_normalized.get(i, 0)
            vector_score = vector_scores_normalized.get(i, 0)
            combined_scores[i] = bm25_weight * bm25_score + vector_weight * vector_score
        
        # Sort and return top_k
        sorted_results = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
        
        results = []
        for doc_id, score in sorted_results[:top_k]:
            chunk = self.chunks[doc_id]
            chunk.bm25_score = bm25_scores_normalized.get(doc_id, 0)
            chunk.vector_score = vector_scores_normalized.get(doc_id, 0)
            chunk.final_score = score
            
            result = SearchResult(
                chunk=chunk,
                score=score,
                source=chunk.metadata.get('source', 'unknown'),
                relevance=score
            )
            results.append(result)
        
        logger.info(f"Found {len(results)} results")
        return results
    
    def _cosine_similarity(self, query_emb: np.ndarray, doc_embs: np.ndarray) -> np.ndarray:
        """计算余弦相似度"""
        query_norm = np.linalg.norm(query_emb)
        doc_norms = np.linalg.norm(doc_embs, axis=1)
        
        similarities = np.dot(doc_embs, query_emb) / (doc_norms * query_norm + 1e-8)
        return similarities
    
    def _normalize_scores(self, scores: Dict[int, float]) -> Dict[int, float]:
        """归一化分数"""
        if not scores:
            return {}
        
        max_score = max(scores.values())
        min_score = min(scores.values())
        
        if max_score == min_score:
            return {k: 0.5 for k in scores.keys()}
        
        return {k: (v - min_score) / (max_score - min_score) for k, v in scores.items()}


# ----------------------------
# Reranking Module
# ----------------------------

class Reranker:
    """重排序器 - 多阶段重排序"""
    
    def __init__(self, model_name: str = 'cross-encoder/ms-marco-MiniLM-L-6-v2'):
        self.model_name = model_name
        self.reranker = None
        self._load_model()
    
    def _load_model(self):
        """加载重排序模型"""
        try:
            from sentence_transformers import CrossEncoder
            self.reranker = CrossEncoder(self.model_name)
            logger.info(f"Loaded reranker model: {self.model_name}")
        except ImportError:
            logger.warning("CrossEncoder not installed. Using simple reranking.")
            self.reranker = None
    
    def rerank(self, query: str, results: List[SearchResult], top_k: int = 5) -> List[SearchResult]:
        """重排序"""
        if not results or self.reranker is None:
            # Return top_k by original score
            return sorted(results, key=lambda x: x.score, reverse=True)[:top_k]
        
        logger.info(f"Reranking {len(results)} results")
        
        # Prepare pairs for reranking
        pairs = [[query, result.chunk.content] for result in results]
        
        # Get reranking scores
        scores = self.reranker.predict(pairs)
        
        # Update scores
        for result, score in zip(results, scores):
            result.relevance = float(score)
        
        # Sort by relevance
        reranked_results = sorted(results, key=lambda x: x.relevance, reverse=True)
        
        logger.info(f"Reranked to {len(reranked_results)} results")
        return reranked_results[:top_k]


# ----------------------------
# Main RAG Pipeline
# ----------------------------

class CodeRepairRAGPipeline:
    """代码修复 RAG 流水线"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.data_collector = CodeDataCollector()
        self.data_cleaner = DataCleaner()
        self.chunking_strategy = ChunkingStrategy(
            method=self.config.get('chunking_method', 'semantic'),
            chunk_size=self.config.get('chunk_size', 512),
            overlap=self.config.get('overlap', 50)
        )
        self.embedding_model = EmbeddingModel()
        self.hybrid_search = HybridSearch(self.embedding_model)
        self.reranker = Reranker()
        self.chunks = []
    
    def build_index(self, 
                   github_queries: List[str] = None,
                   local_repos: List[str] = None,
                   output_dir: str = "./data/processed"):
        """构建索引"""
        logger.info("Building RAG index...")
        
        # 1. Data Collection
        all_snippets = []
        
        if github_queries:
            for query in github_queries:
                snippets = self.data_collector.collect_from_github(query)
                all_snippets.extend(snippets)
        
        if local_repos:
            snippets = self.data_collector.collect_from_local_repos(local_repos)
            all_snippets.extend(snippets)
        
        logger.info(f"Collected {len(all_snippets)} total snippets")
        
        # Save raw data
        self.data_collector.save(all_snippets, "raw_data.json")
        
        # 2. Data Cleaning
        cleaned_snippets = self.data_cleaner.deduplicate(all_snippets, method='normalized')
        cleaned_snippets = self.data_cleaner.remove_noise(cleaned_snippets)
        cleaned_snippets = self.data_cleaner.filter_by_quality(cleaned_snippets)
        
        # Save cleaned data
        self.data_collector.save(cleaned_snippets, "cleaned_data.json")
        
        # 3. Chunking
        all_chunks = []
        for snippet in cleaned_snippets:
            metadata = {
                'source': snippet.source,
                'language': snippet.language,
                'tags': snippet.tags,
                'bug_type': snippet.bug_type
            }
            chunks = self.chunking_strategy.chunk(snippet.code, metadata)
            all_chunks.extend(chunks)
        
        logger.info(f"Created {len(all_chunks)} chunks")
        self.chunks = all_chunks
        
        # 4. Indexing
        self.hybrid_search.index(all_chunks)
        
        # 5. Save index
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        with open(output_path / "chunks.pkl", 'wb') as f:
            pickle.dump(all_chunks, f)
        
        logger.info(f"Index built and saved to {output_path}")
    
    def search(self, query: str, top_k: int = 10, use_rerank: bool = True) -> List[SearchResult]:
        """检索"""
        # Hybrid search
        results = self.hybrid_search.search(query, top_k=top_k * 2)
        
        # Rerank
        if use_rerank and self.reranker:
            results = self.reranker.rerank(query, results, top_k=top_k)
        else:
            results = results[:top_k]
        
        return results
    
    def load_index(self, index_path: str = "./data/processed"):
        """加载索引"""
        chunks_path = Path(index_path) / "chunks.pkl"
        
        if not chunks_path.exists():
            raise FileNotFoundError(f"Index not found at {chunks_path}")
        
        with open(chunks_path, 'rb') as f:
            self.chunks = pickle.load(f)
        
        self.hybrid_search.index(self.chunks)
        logger.info(f"Loaded index with {len(self.chunks)} chunks")


# ----------------------------
# Example Usage
# ----------------------------

if __name__ == "__main__":
    # Example: Build index
    config = {
        'chunking_method': 'semantic',
        'chunk_size': 512,
        'overlap': 50
    }
    
    pipeline = CodeRepairRAGPipeline(config)
    
    # Build index from GitHub and local repos
    pipeline.build_index(
        github_queries=['python bug fix', 'code repair'],
        local_repos=['./requests'],
        output_dir="./data/processed"
    )
    
    # Search
    results = pipeline.search("fix iter_slices function bug", top_k=5)
    
    print(f"\nTop {len(results)} results:")
    for i, result in enumerate(results, 1):
        print(f"\n{i}. Score: {result.score:.4f}, Relevance: {result.relevance:.4f}")
        print(f"   Source: {result.source}")
        print(f"   Content: {result.chunk.content[:200]}...")