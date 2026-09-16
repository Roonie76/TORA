# TORA Phase 2H Architecture: Research Tool Foundation

## 1. Executive Overview
Phase 2H establishes the **Research Tool Foundation** for TORA. Prior to Phase 2H, TORA possessed distinct tools for web search (`WebSearchTool` via `SearchProvider`) and web fetch (`WebFetchTool` via `FetchProvider`). Phase 2H-A creates a unified, provider-agnostic, strongly typed **Research Abstraction** (`ResearchProvider`) capable of executing searches, fetching content, enriching sources with metadata and timestamps, deduplicating findings, and validating sources independently of any LLM, framework, or transport layer.

---

## 2. Current Architecture Inspection

### 2.1 Web-Search Architecture
- **Location:** `backend/tools/search/` and `backend/tools/web_search.py`
- **Abstract Provider:** `SearchProvider` (`backend/tools/search/base.py`) defines `async def search(query: str, max_results: int = 5) -> SearchResponse`.
- **Implementations:**
  - `DuckDuckGoSearchProvider` (`backend/tools/search/duckduckgo.py`): HTML scraping with rate-limit backoff and region targeting.
  - `TavilySearchProvider` (`backend/tools/search/tavily.py`): API integration for structured JSON search.
- **Factory:** `get_search_provider()` resolves provider based on `SEARCH_PROVIDER` and `TAVILY_API_KEY` environment variables.
- **Tool Wrapper:** `WebSearchTool` (`backend/tools/web_search.py`) inherits from `BaseTool`, validating input with `WebSearchInput` (max 300 chars query, 1-10 results).

### 2.2 HTTP-Fetch Architecture
- **Location:** `backend/tools/web_fetch/`
- **Abstract Provider:** `FetchProvider` (`backend/tools/web_fetch/provider/base.py`) defines `async def fetch(url: str, max_chars: int = 3000, timeout_seconds: float = 10.0) -> FetchResponse`.
- **Implementation:** `HttpFetchProvider` (`backend/tools/web_fetch/provider/http.py`) with strict SSRF defense (private/loopback/cloud metadata IP blocking, DNS resolution pinning, redirect re-validation), size caps (2MB), and content extraction (`HtmlExtractor`).
- **Tool Wrapper:** `WebFetchTool` (`backend/tools/web_fetch/tool.py`) wraps `FetchProvider` for execution in `ToolExecutor`.

### 2.3 Existing Tool Interfaces
- All tools inherit from `BaseTool` (`backend/tools/base.py`) with:
  - `name: str`
  - `description: str`
  - `args_schema: Type[BaseModel]`
  - `metadata: ToolMetadata`
  - `async def execute(**kwargs) -> Dict[str, Any]`
- Execution is orchestrated via `ToolRegistry` and `ToolExecutor` which wraps outputs in `ToolResult`.

### 2.4 How Search/Fetch Results Currently Reach ToraAgent
1. **User Query:** User asks a real-time question (e.g. *"What are current SBI gold loan interest rates?"*).
2. **Planner:** `Planner` inspects registered tools and outputs `ToolPlan(requires_tools=True, steps=[ToolPlanStep(tool_name="web_search", arguments={"query": "SBI gold loan interest rates 2026"})])`.
3. **Execution:** `ToraAgent` invokes `ToolExecutor.execute()`.
4. **Context Compaction & Assembly:** `ToolExecutor.to_tool_context()` records `ToolResult` in `ToolContext`. `ContextBuilder` renders tool outputs under `## Tool Execution Results` with strict untrusted data demarcation.
5. **LLM Generation:** `ToraAgent` passes context to `LLMProvider` for grounded response synthesis.

---

## 3. Existing Limitations

1. **Fragmented Abstractions:** Search and Fetch operate as isolated tools with no shared concept of a "Research Source" or source lifecycle.
2. **Missing Research Metadata:** Existing `SearchResult` and `FetchResult` lack uniform retrieval timestamps, source publication dates, deduplication logic, and verification provenance.
3. **No Unified Research Provider:** No single abstraction allows higher-level research workflows (multi-source comparison, verification, deep research) to query both search and fetch via a unified interface.
4. **Error Surface Coupling:** Tool errors in web search and web fetch return raw dicts without unified research error taxonomy.

---

## 4. Phase 2H-A Research Abstraction Design

### 4.1 Core Abstraction: `ResearchProvider`
```
                    ResearchProvider (ABC)
                    ├── search(query, max_results) -> ResearchSearchResponse
                    └── fetch(url, max_chars) -> ResearchFetchResult
                                │
                                ▼
                    CompositeResearchProvider
                    ├── SearchProvider (DuckDuckGo / Tavily)
                    └── FetchProvider (HttpFetchProvider)
```

### 4.2 Strongly Typed Models (`backend/research/models.py`)
1. **`SourceMetadata`**:
   - `url: str`
   - `title: Optional[str]`
   - `domain: Optional[str]`
   - `source_name: Optional[str]`
   - `retrieved_at: str` (ISO 8601 UTC timestamp)
   - `published_at: Optional[str]`
   - `author: Optional[str]`
   - `content_type: Optional[str]`
   - `is_verified: bool`
   - `extra: Dict[str, Any]`

2. **`ResearchSearchResult`**:
   - `query: str`
   - `title: str`
   - `url: str`
   - `domain: str`
   - `snippet: str`
   - `rank: Optional[int]`
   - `metadata: SourceMetadata`
   - `success: bool`
   - `error: Optional[str]`

3. **`ResearchFetchResult`**:
   - `url: str`
   - `final_url: str`
   - `title: str`
   - `domain: str`
   - `content: str`
   - `content_type: str`
   - `status_code: int`
   - `truncated: bool`
   - `metadata: SourceMetadata`
   - `success: bool`
   - `error: Optional[str]`

4. **`ResearchSearchResponse`**:
   - `query: str`
   - `results: List[ResearchSearchResult]`
   - `total_results: int`
   - `provider: str`
   - `success: bool`
   - `error: Optional[str]`
   - `retrieved_at: str`

### 4.3 Key Architectural Invariants
- **Zero Direct Dependencies on LLMs or Frameworks:** The research package must not import Ollama, Gemini, OpenAI, FastAPI, or UI components.
- **Independent Testability:** Research provider can be tested via mock providers without network access.
- **Zero Regression:** Existing `WebSearchTool`, `WebFetchTool`, `SearchProvider`, `FetchProvider`, and agent workflows remain completely intact.
- **Deterministic Enrichment:** Automatic UTC retrieval timestamps, domain extraction, URL deduplication, and schema validation.

---

## 5. Components to Remain Unchanged
- `backend/tools/base.py`
- `backend/tools/calculator.py`
- `backend/tools/search/base.py`, `duckduckgo.py`, `tavily.py`
- `backend/tools/web_fetch/extractor.py`, `models.py`, `provider/http.py`
- `backend/context/` (All memory, token budget, and context compaction mechanisms)
- `backend/planner/` (Tool planning loop)
- `backend/agent/agent.py` (Core agent execution loop)
