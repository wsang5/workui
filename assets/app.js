    // 页面启动时通过本地服务从飞书表格拉取数据；问题分析按表头列名写回对应实验组列。
    let rows = [];
    let sourceConfig = { activeUrl: "", sources: [] };
    let activeSource = { title: "飞书表格", url: "" };

    const pageSize = 100;
    const randomSortInterval = 5;
    const annotationStatusOptions = ["全部", "机标未校验", "已标注", "未标注"];
    const adjustmentFlagOptions = ["全部", "有调整"];
    const allIssueModelsValue = "__all_issue_models__";
    const allIssueLabelsValue = "__all_issue_labels__";
    const searchScopeOptions = [
      { value: "prompt", label: "原始 Prompt 内容" },
      { value: "peAnnotation", label: "PE 标注内容" }
    ];
    const tagKeys = ["输入图数量", "场景", "语种", "题库类型", "任务类型", "结构", "美感", "响应"];
    let labelTaxonomy = {
      imageEffectLabels: [],
      peProblemLabels: [],
      i2iImageEffectLabels: [],
      i2iPeProblemLabels: [],
      taxonomies: {
        t2i: { imageEffectLabels: [], peProblemLabels: [] },
        i2i: { imageEffectLabels: [], peProblemLabels: [] }
      }
    };
    const state = {
      annotationStatus: "全部",
      tag: "全部标签",
      issueModel: allIssueModelsValue,
      adjustmentFlag: "全部",
      issueLabel: allIssueLabelsValue,
      searchScope: "prompt",
      query: "",
      sortMode: "ordered",
      filtered: [],
      page: 1,
      currentIndex: 0,
      listViewMode: "card",
      listScrollTop: 0,
      listAnchorId: "",
      listAnchorTop: 0,
      annotationDraft: null,
      distributionOrder: [],
      distributionLeftModel: "",
      distributionRightModel: "",
      selectedPromptGroupIds: [],
      workMode: "browse",
      qcReviewer: "",
      qcStatus: "全部",
      supportsQC: false,
      qcStats: null,
      qcConfig: { T: 0.80, R1: 0.30, R2: 0.50 }
    };

    const qcStatusOptions = ["全部", "未检", "已检", "不通过"];
    const QC_PASS = "通过";
    const QC_FAIL = "不通过";

    // 性能优化：缓存
    const cache = {
      issueModelOptions: null,
      issueLabelOptions: new Map(),
      issueDistributionStats: null,
      tagOptions: null,
      lastFilterState: null,
      lastRowsHash: null
    };

    // 性能优化：防抖 timer
    let searchDebounceTimer = null;

    const $ = (id) => document.getElementById(id);
    const urlPattern = /(https?:\/\/[^\s"'<>]+)/g;

    function saveQcUiState() {
      localStorage.setItem("peWorkMode", state.workMode);
      localStorage.setItem("workMode", state.workMode);
      localStorage.setItem("qcStatus", state.qcStatus);
      if (state.qcReviewer) localStorage.setItem("qcReviewer", state.qcReviewer);
    }

    function restoreQcUiState() {
      state.qcReviewer = localStorage.getItem("qcReviewer") || "";
      const savedMode = localStorage.getItem("peWorkMode") || localStorage.getItem("workMode") || "";
      state.workMode = savedMode === "qc" ? "qc" : "browse";
      const savedStatus = localStorage.getItem("qcStatus") || "全部";
      state.qcStatus = qcStatusOptions.includes(savedStatus) ? savedStatus : "全部";
      try {
        const savedConfig = JSON.parse(localStorage.getItem("qcConfig") || "{}");
        state.qcConfig = { ...state.qcConfig, ...savedConfig };
      } catch {
        localStorage.removeItem("qcConfig");
      }
    }

    // 计算数据 hash（用于判断是否需要清除缓存）
    function getRowsHash() {
      return rows.length + '-' + rows.slice(0, 100).map(r => r.id).join(',');
    }

    // 清除缓存（数据变化时调用）
    function clearCache() {
      cache.issueModelOptions = null;
      cache.issueLabelOptions.clear();
      cache.issueDistributionStats = null;
      cache.tagOptions = null;
      cache.lastRowsHash = getRowsHash();
    }

    // 检查筛选状态是否变化
    function filterStateChanged() {
      const currentState = JSON.stringify({
        annotationStatus: state.annotationStatus,
        tag: state.tag,
        issueModel: state.issueModel,
        adjustmentFlag: state.adjustmentFlag,
        issueLabel: state.issueLabel,
        searchScope: state.searchScope,
        query: state.query
      });
      if (cache.lastFilterState === currentState) return false;
      cache.lastFilterState = currentState;
      return true;
    }

    async function init() {
      restoreQcUiState();
      bindEvents();
      await loadSources();
      await Promise.all([loadLabelTaxonomy(), loadRowsFromLark()]);
    }

    async function loadLabelTaxonomy() {
      try {
        const response = await fetch("/api/label-taxonomy");
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "读取标签体系失败");
        labelTaxonomy = normalizeLabelTaxonomy(payload);
      } catch (error) {
        console.warn(error);
        toast(`标签体系加载失败：${error.message}`);
      }
    }

    function normalizeLabelTaxonomy(payload = {}) {
      const t2i = payload.taxonomies?.t2i || {};
      const i2i = payload.taxonomies?.i2i || {};
      const t2iImage = t2i.imageEffectLabels || payload.imageEffectLabels || [];
      const t2iPe = t2i.peProblemLabels || payload.peProblemLabels || [];
      const i2iImage = i2i.imageEffectLabels || payload.i2iImageEffectLabels || [];
      const i2iPe = i2i.peProblemLabels || payload.i2iPeProblemLabels || [];
      return {
        imageEffectLabels: t2iImage,
        peProblemLabels: t2iPe,
        i2iImageEffectLabels: i2iImage,
        i2iPeProblemLabels: i2iPe,
        taxonomies: {
          t2i: { imageEffectLabels: t2iImage, peProblemLabels: t2iPe },
          i2i: { imageEffectLabels: i2iImage, peProblemLabels: i2iPe }
        }
      };
    }

    async function loadSources() {
      const response = await fetch(`/api/sources?ts=${Date.now()}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "读取数据源配置失败");
      sourceConfig = payload;
      activeSource = sourceConfig.sources.find((item) => item.url === sourceConfig.activeUrl) || sourceConfig.sources[0] || activeSource;
      renderSources();
    }

    async function loadRowsFromLark() {
      if (!sourceConfig.sources || !sourceConfig.sources.length) {
        $("listSummary").textContent = "尚未配置数据源";
        $("resultList").innerHTML = `
          <div class="panel">
            <div class="panel-body empty">
              请在页面顶部粘贴飞书表格（/sheets/）或多维表格（/base/）链接后点击「加载」。
            </div>
          </div>
        `;
        $("pagination").innerHTML = "";
        return;
      }
      $("listSummary").textContent = "正在从飞书加载数据...";
      $("resultList").innerHTML = `<div class="panel"><div class="panel-body empty">正在读取：${escapeHtml(activeSource.title || activeSource.url || "飞书数据源")}</div></div>`;
      $("pagination").innerHTML = "";

      try {
        const response = await fetch("/api/rows", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "读取飞书数据源失败");
        rows = payload.rows || [];
        activeSource = payload.sourceInfo || activeSource;
        state.supportsQC = !!(payload.columnMapping && payload.columnMapping.supportsQC);
        clearCache(); // 数据加载后清除缓存
        renderQcControls();
        applyFilters();
        renderSources();
        restoreDetailFromHash();
        toast(`已加载：${activeSource.title || "飞书数据源"}`);
      } catch (error) {
        $("listSummary").textContent = "飞书数据源加载失败";
        $("resultList").innerHTML = `
          <div class="panel">
            <div class="panel-body empty">
              请通过本地服务打开页面：python3 lark_server.py。错误：${escapeHtml(error.message)}
            </div>
          </div>
        `;
      }
    }

    function renderSources() {
      $("sourceSelect").innerHTML = (sourceConfig.sources || []).map((source) => `
        <option value="${escapeAttr(source.url)}" ${source.url === sourceConfig.activeUrl ? "selected" : ""}>${escapeHtml(source.title || source.url)}</option>
      `).join("");
    }

    function renderQcControls() {
      $("qcBar").hidden = !state.supportsQC;
      $("qcModeToggle").checked = state.workMode === "qc";
      
      const reviewerDisplay = $("qcReviewerDisplay");
      if (state.workMode === "qc" && state.qcReviewer) {
        reviewerDisplay.hidden = false;
        $("qcReviewerName").textContent = state.qcReviewer;
      } else {
        reviewerDisplay.hidden = true;
      }

      const showQcStatus = state.supportsQC && state.workMode === "qc";
      const filter = $("qcStatusFilter");
      filter.hidden = !showQcStatus;
      if (showQcStatus) {
        filter.innerHTML = qcStatusOptions.map((value) => `
          <option value="${escapeHtml(value)}" ${value === state.qcStatus ? "selected" : ""}>质检：${escapeHtml(value)}</option>
        `).join("");
      }
      $("distributionBtn").textContent = state.workMode === "qc" ? "质检统计" : "问题分布";
    }

    function qcStatusForRow(row) {
      if (!row.qcChecked) return "未检";
      return row.qcVerdict === QC_FAIL ? "不通过" : "已检";
    }

    function bindEvents() {
      $("annotationStatusFilter").addEventListener("change", (event) => {
        state.annotationStatus = event.target.value;
        state.page = 1;
        applyFilters();
      });

      $("tagFilter").addEventListener("change", (event) => {
        state.tag = event.target.value;
        state.page = 1;
        applyFilters();
      });

      $("issueModelFilter").addEventListener("change", (event) => {
        state.issueModel = event.target.value;
        state.issueLabel = allIssueLabelsValue;
        state.page = 1;
        applyFilters();
      });

      $("adjustmentFlagFilter").addEventListener("change", (event) => {
        state.adjustmentFlag = event.target.value;
        state.page = 1;
        applyFilters();
      });

      $("issueLabelFilter").addEventListener("change", (event) => {
        state.issueLabel = event.target.value;
        state.page = 1;
        applyFilters();
      });

      $("qcStatusFilter").addEventListener("change", (event) => {
        state.qcStatus = event.target.value;
        saveQcUiState();
        state.page = 1;
        applyFilters();
      });

      $("qcSwitchUserBtn").addEventListener("click", () => {
        $("qcLoginNameInput").value = state.qcReviewer;
        $("qcLoginOverlay").hidden = false;
        $("qcLoginNameInput").focus();
      });

      $("qcLoginCancelBtn").addEventListener("click", () => {
        $("qcLoginOverlay").hidden = true;
      });

      $("qcLoginForm").addEventListener("submit", (event) => {
        event.preventDefault();
        const name = $("qcLoginNameInput").value.trim();
        if (!name) {
          toast("姓名不能为空");
          return;
        }
        state.qcReviewer = name;
        $("qcLoginOverlay").hidden = true;
        
        if (state.workMode !== "qc") {
          state.workMode = "qc";
          state.page = 1;
          applyFilters();
        }
        saveQcUiState();
        renderQcControls();
      });

      $("qcModeToggle").addEventListener("change", (event) => {
        if (event.target.checked && !state.qcReviewer) {
          event.target.checked = false;
          $("qcLoginNameInput").value = "";
          $("qcLoginOverlay").hidden = false;
          $("qcLoginNameInput").focus();
          return;
        }
        state.workMode = event.target.checked ? "qc" : "browse";
        saveQcUiState();
        state.page = 1;
        renderQcControls();
        applyFilters();
      });

      $("distributionBtn").addEventListener("click", showDistribution);
      $("distributionBackBtn").addEventListener("click", showList);
      $("distributionLeftModel").addEventListener("change", (event) => {
        state.distributionLeftModel = event.target.value;
        renderIssueDistribution();
      });
      $("distributionRightModel").addEventListener("change", (event) => {
        state.distributionRightModel = event.target.value;
        renderIssueDistribution();
      });
      $("distributionSwapBtn").addEventListener("click", () => {
        [state.distributionLeftModel, state.distributionRightModel] = [state.distributionRightModel, state.distributionLeftModel];
        renderIssueDistribution();
      });
      $("issueCompareChart").addEventListener("click", (event) => {
        const button = event.target.closest("[data-issue-compare-tag]");
        if (!button) return;
        state.issueModel = button.dataset.issueCompareModel || allIssueModelsValue;
        state.issueLabel = button.dataset.issueCompareTag || allIssueLabelsValue;
        state.page = 1;
        applyFilters();
        showList();
      });

      $("searchScopeFilter").addEventListener("change", (event) => {
        state.searchScope = event.target.value;
        state.page = 1;
        $("searchInput").placeholder = state.searchScope === "peAnnotation" ? "搜索 PE 标注内容" : "搜索原始输入内容";
        applyFilters();
      });

      $("searchInput").addEventListener("input", (event) => {
        state.query = event.target.value.trim().toLowerCase();
        state.page = 1;

        // 性能优化：搜索防抖 300ms
        if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => {
          applyFilters();
        }, 300);
      });

      $("sortModeSelect").addEventListener("change", (event) => {
        state.sortMode = event.target.value;
        state.page = 1;
        applyFilters();
      });

      $("sourceSelect").addEventListener("change", async (event) => {
        const selected = sourceConfig.sources.find((item) => item.url === event.target.value);
        if (selected) activeSource = selected;
        await switchSource({ activeUrl: event.target.value });
      });

      $("sourceForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const url = $("sourceUrlInput").value.trim();
        if (!url) {
          toast("请先粘贴飞书表格链接");
          return;
        }
        await switchSource({ url });
        $("sourceUrlInput").value = "";
      });

      $("sourceDeleteBtn").addEventListener("click", async () => {
        const target = $("sourceSelect").value || sourceConfig.activeUrl;
        if (!target) {
          toast("当前没有可删除的数据源");
          return;
        }
        const source = sourceConfig.sources.find((item) => item.url === target);
        if (!window.confirm(`确认删除数据源「${source?.title || target}」？该操作仅移除本地保存的连接，不影响飞书表格本身。`)) return;
        await removeSource(target);
      });

      $("viewSwitch").addEventListener("click", (event) => {
        const button = event.target.closest("button[data-list-view]");
        if (!button) return;
        state.listViewMode = button.dataset.listView;
        renderList();
      });

      $("backBtn").addEventListener("click", showList);
      $("prevBtn").addEventListener("click", () => moveDetail(-1));
      $("nextBtn").addEventListener("click", () => moveDetail(1));
      $("sidePrevBtn").addEventListener("click", () => moveDetail(-1));
      $("sideNextBtn").addEventListener("click", () => moveDetail(1));
      $("issuePanel").addEventListener("click", handleIssuePanelClick);
      $("issuePanel").addEventListener("mousedown", handleIssuePanelMouseDown);
      $("issuePanel").addEventListener("input", handleIssuePanelInput);
      $("issuePanel").addEventListener("keydown", handleIssuePanelKeydown);
      $("issuePanel").addEventListener("focusin", handleIssuePanelFocusIn);
      $("issuePanel").addEventListener("focusout", handleIssuePanelFocusOut);
      $("compareGrid").addEventListener("mouseup", handlePePromptSelection);
      $("compareGrid").addEventListener("click", handlePromptComparisonClick);
      $("compareGrid").addEventListener("change", handlePromptGroupSelection);
      $("saveAnnotationBtn").addEventListener("click", saveAnnotationFromPopover);
      $("deleteAnnotationBtn").addEventListener("click", deleteAnnotationFromPopover);
      $("cancelAnnotationBtn").addEventListener("click", closeAnnotationPopover);
      $("closeImageViewer").addEventListener("click", closeImageViewer);
      $("imageViewer").addEventListener("click", (event) => {
        if (event.target.id === "imageViewer") closeImageViewer();
      });
      document.addEventListener("pointerdown", handleIssueOptionPointerDown, true);

      $("annotationReason").addEventListener("keydown", (event) => {
        if (event.isComposing || event.keyCode === 229) return;
        if (event.key !== "Enter" || event.shiftKey) return;
        event.preventDefault();
        saveAnnotationFromPopover();
      });

      document.addEventListener("click", (event) => {
        const image = event.target.closest(".media-grid img, .row-thumb img, .mini-image");
        if (!image) return;
        openImageViewer(image.src, image.alt);
      });

      document.addEventListener("mousedown", (event) => {
        if (!$("annotationPopover").classList.contains("open")) return;
        if ($("annotationPopover").contains(event.target) || $("compareGrid").contains(event.target)) return;
        closeAnnotationPopover();
      });

      document.addEventListener("keydown", (event) => {
        if ($("imageViewer").classList.contains("open")) {
          if (event.key === "Escape") closeImageViewer();
          return;
        }
        if ($("annotationPopover").classList.contains("open")) {
          if (event.key === "Escape") closeAnnotationPopover();
          return;
        }
        if ($("distributionView").classList.contains("active")) {
          if (event.key === "Escape") showList();
          return;
        }
        if (!$("detailView").classList.contains("active")) return;
        if (isTypingTarget(event.target) || event.isComposing || event.keyCode === 229) return;
        if (event.key === "ArrowLeft") moveDetail(-1);
        if (event.key === "ArrowRight") moveDetail(1);
        if (event.key === "Escape") showList();
      });
    }

    async function switchSource(payload) {
      const button = $("sourceForm").querySelector('button[type="submit"]');
      const select = $("sourceSelect");
      button.disabled = true;
      select.disabled = true;
      try {
        const response = await fetch("/api/sources", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "切换飞书表格失败");
        sourceConfig = { activeUrl: result.activeUrl, sources: result.sources || [] };
        activeSource = sourceConfig.sources.find((item) => item.url === sourceConfig.activeUrl) || activeSource;
        rows = [];
        state.filtered = [];
        state.page = 1;
        state.currentIndex = 0;
        showList();
        renderSources();
        await loadRowsFromLark();
      } catch (error) {
        toast(`数据源切换失败：${error.message}`);
        renderSources();
      } finally {
        button.disabled = false;
        select.disabled = false;
      }
    }

    async function removeSource(removeUrl) {
      const deleteBtn = $("sourceDeleteBtn");
      const select = $("sourceSelect");
      deleteBtn.disabled = true;
      select.disabled = true;
      try {
        const response = await fetch("/api/sources", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ removeUrl })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "删除数据源失败");
        sourceConfig = { activeUrl: result.activeUrl, sources: result.sources || [] };
        activeSource = sourceConfig.sources.find((item) => item.url === sourceConfig.activeUrl) || sourceConfig.sources[0] || activeSource;
        rows = [];
        state.filtered = [];
        state.page = 1;
        state.currentIndex = 0;
        showList();
        renderSources();
        toast("已删除数据源");
        await loadRowsFromLark();
      } catch (error) {
        toast(`删除失败：${error.message}`);
        renderSources();
      } finally {
        deleteBtn.disabled = false;
        select.disabled = false;
      }
    }

    function isTypingTarget(target) {
      if (!target) return false;
      const tagName = target.tagName;
      return target.isContentEditable || tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT";
    }

    function applyFilters() {
      if (!annotationStatusOptions.includes(state.annotationStatus)) state.annotationStatus = "全部";
      if (!searchScopeOptions.some((option) => option.value === state.searchScope)) state.searchScope = "prompt";
      if (!["ordered", "intervalRandom"].includes(state.sortMode)) state.sortMode = "ordered";
      $("sortModeSelect").value = state.sortMode;

      $("annotationStatusFilter").innerHTML = annotationStatusOptions.map((value) => `
        <option value="${escapeHtml(value)}" ${value === state.annotationStatus ? "selected" : ""}>${escapeHtml(value)}</option>
      `).join("");

      $("searchScopeFilter").innerHTML = searchScopeOptions.map((option) => `
        <option value="${escapeHtml(option.value)}" ${option.value === state.searchScope ? "selected" : ""}>${escapeHtml(option.label)}</option>
      `).join("");

      const tagOptions = getTagOptions();
      if (!tagOptions.includes(state.tag)) state.tag = "全部标签";
      $("tagFilter").innerHTML = tagOptions.map((value) => `
        <option value="${escapeHtml(value)}" ${value === state.tag ? "selected" : ""}>${escapeHtml(value)}</option>
      `).join("");

      const issueModels = issueModelOptions();
      if (!issueModels.some((option) => option.value === state.issueModel)) state.issueModel = allIssueModelsValue;
      $("issueModelFilter").innerHTML = issueModels.map((option) => `
        <option value="${escapeHtml(option.value)}" ${option.value === state.issueModel ? "selected" : ""}>${escapeHtml(option.label)}</option>
      `).join("");

      if (!adjustmentFlagOptions.includes(state.adjustmentFlag)) state.adjustmentFlag = "全部";
      $("adjustmentFlagFilter").innerHTML = adjustmentFlagOptions.map((value) => `
        <option value="${escapeHtml(value)}" ${value === state.adjustmentFlag ? "selected" : ""}>${escapeHtml(value)}</option>
      `).join("");

      const issueLabels = issueLabelOptions(state.issueModel);
      if (!issueLabels.some((option) => option.value === state.issueLabel)) state.issueLabel = allIssueLabelsValue;
      $("issueLabelFilter").innerHTML = issueLabels.map((option) => `
        <option value="${escapeHtml(option.value)}" ${option.value === state.issueLabel ? "selected" : ""}>${escapeHtml(option.label)}</option>
      `).join("");

      const matchedRows = rows.filter((row) => rowMatchesFilters(row));
      state.filtered = sortRows(matchedRows);

      const pages = totalPages();
      if (state.page > pages) state.page = pages;

      renderList();
      renderIssueDistribution();
    }

    function renderList() {
      const pages = totalPages();
      const start = (state.page - 1) * pageSize;
      const pageRows = state.filtered.slice(start, start + pageSize);
      const pageStart = state.filtered.length ? start + 1 : 0;
      const pageEnd = start + pageRows.length;
      const abMode = state.filtered.some((row) => row.mode === "ab-eval") || rows.some((row) => row.mode === "ab-eval");
      const statusText = abMode || state.annotationStatus === "全部" ? "" : `，${state.annotationStatus}`;
      const issueText = abMode ? "" : issueFilterSummary();
      $("listSummary").textContent = `当前 全部数据${statusText}${issueText}，共 ${state.filtered.length} 条，显示 ${pageStart}-${pageEnd}`;
      renderViewSwitch();

      if (!state.filtered.length) {
        $("resultList").className = "list";
        $("resultList").innerHTML = `<div class="panel"><div class="panel-body empty">没有匹配结果，请调整筛选条件。</div></div>`;
        $("pagination").innerHTML = "";
        return;
      }

      if (state.listViewMode === "waterfall") {
        $("resultList").className = "list waterfall";
        $("resultList").innerHTML = pageRows.map((row, index) =>
          row.mode === "ab-eval" ? renderAbEvalCard(row, start + index + 1) : renderWaterfallCard(row, start + index + 1)
        ).join("");
        $("resultList").querySelectorAll(".waterfall-card").forEach((card) => {
          card.addEventListener("click", (event) => {
            if (event.target.closest(".mini-image")) return;
            const index = state.filtered.findIndex((row) => row.id === card.dataset.id);
            showDetail(index);
          });
        });
        renderPagination(pages);
        return;
      }

      $("resultList").className = "list";
      $("resultList").innerHTML = pageRows.map((row, index) => `
        <button type="button" class="row-card" data-id="${row.id}">
          <span class="index">${start + index + 1}</span>
          ${renderListThumb(row.c)}
          <span class="row-main">
            <span class="row-title">${escapeHtml(stripUrls(row.c))}</span>
          </span>
          ${row.mode === "ab-eval" ? renderAbEvalRowChips(row) : renderTagChips(row, 8)}
        </button>
      `).join("");

      $("resultList").querySelectorAll(".row-card").forEach((button) => {
        button.addEventListener("click", () => {
          const index = state.filtered.findIndex((row) => row.id === button.dataset.id);
          showDetail(index);
        });
      });

      renderPagination(pages);
    }

    function renderViewSwitch() {
      $("viewSwitch").querySelectorAll("button[data-list-view]").forEach((button) => {
        button.classList.toggle("active", button.dataset.listView === state.listViewMode);
      });
    }

    function sortRows(items) {
      if (state.sortMode !== "intervalRandom") return items;
      const sorted = [];
      for (let offset = 0; offset < randomSortInterval; offset += 1) {
        for (let index = offset; index < items.length; index += randomSortInterval) {
          sorted.push(items[index]);
        }
      }
      return sorted;
    }

    function showList() {
      $("detailView").classList.remove("active");
      $("distributionView").classList.remove("active");
      $("listView").classList.add("active");
      document.body.classList.remove("detail-mode");
      clearDetailHash();
      renderList();
      restoreListPosition();
    }

    function showDistribution() {
      rememberListPosition();
      $("detailView").classList.remove("active");
      $("listView").classList.remove("active");
      $("distributionView").classList.add("active");
      document.body.classList.remove("detail-mode");
      clearDetailHash();
      if (state.workMode === "qc" && state.supportsQC) {
        $("issueCompareChart").hidden = true;
        $("qcStatsView").hidden = false;
        renderQcStats();
      } else {
        $("issueCompareChart").hidden = false;
        $("qcStatsView").hidden = true;
        renderIssueDistribution();
      }
      window.scrollTo({ top: 0, behavior: "auto" });
    }

    async function renderQcStats() {
      const container = $("qcStatsView");
      container.innerHTML = `<div class="empty">正在统计质检进度...</div>`;
      try {
        const params = new URLSearchParams({
          T: String(state.qcConfig.T),
          R1: String(state.qcConfig.R1),
          R2: String(state.qcConfig.R2)
        });
        const response = await fetch(`/api/qc-stats?${params.toString()}`);
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "统计失败");
        state.qcStats = payload;
        const stateLabel = { PASS: "通过锁定", SUSPECT: "可疑", REWORK: "返工中", PENDING: "待抽检" };
        const stageLabel = { S1: "阶段1：抽30%", S2: "阶段2：补到50%", DONE: "已结束", REWORK: "待返工" };
        const fmtRate = (r) => (r == null ? "—" : `${(r * 100).toFixed(1)}%`);
        const s = payload.summary || {};
        const config = payload.config || payload.params || {};
        state.qcConfig = { ...state.qcConfig, ...config };
        localStorage.setItem("qcConfig", JSON.stringify(state.qcConfig));
        const nextQueue = payload.nextQueue || [];
        const reworkItems = payload.reworkItems || [];
        const rowsHtml = (payload.annotators || []).map((a) => `
          <tr>
            <td>${escapeHtml(a.reviewer)}</td>
            <td>${a.checked} / ${a.total}</td>
            <td>${a.defects}</td>
            <td>${fmtRate(a.passRate)}</td>
            <td>${stageLabel[a.currentStage] || a.currentStage || "—"}</td>
            <td><span class="qc-state qc-state-${a.personState}">${stateLabel[a.personState] || a.personState}</span></td>
            <td>${a.stage1Quota || "—"} / ${a.stage2Quota || "—"}</td>
            <td>${a.distanceToStageDone || "—"}</td>
          </tr>
        `).join("");
        const nextHtml = nextQueue.slice(0, 20).map((it) => `<span class="chip">${escapeHtml(it.reviewer || "")} ${escapeHtml(it.promptId || it.id || "")}</span>`).join("");
        container.innerHTML = `
          <div class="qc-stats-summary">
            <div><strong>${stageLabel[payload.stage] || payload.stage}</strong> · 总体正确率 <strong class="qc-big-rate">${fmtRate(payload.overallAccuracy ?? s.overallAccuracy)}</strong> · 阈值 ${fmtRate(config.T)}</div>
            <div>责任池总量 ${s.totalItems || 0} · 已查 / 全检基线 ${s.totalChecked || 0} / ${s.fullCheckBaseline || s.totalItems || 0} · 省 ${s.savedItems || 0}（${fmtRate(s.savedRate)}）</div>
            <div>状态：通过 ${s.passCount || 0} 人 · 可疑 ${s.suspectCount || 0} 人 · 返工 ${s.reworkCount || 0} 人 · 待抽 ${s.pendingCount || 0} 人</div>
            <div>配置：T=${fmtRate(config.T)}，R1=${fmtRate(config.R1)}，R2=${fmtRate(config.R2)}</div>
          </div>
          <div class="qc-next-panel">
            <strong>下一步该查谁</strong>
            <div>${nextHtml || "—"}</div>
          </div>
          <div class="qc-next-panel">
            <strong>待返工清单</strong>
            <div>${reworkItems.length ? `${reworkItems.length} 条未质检条目待打回` : "无"}</div>
            ${payload.stage === "REWORK" && reworkItems.length ? `<button type="button" id="qcReworkBtn">一键打回返工</button>` : ""}
          </div>
          <form class="qc-config-panel" id="qcConfigForm">
            <label>阈值 T <input type="number" min="0.01" max="0.99" step="0.01" name="T" value="${Number(config.T ?? state.qcConfig.T).toFixed(2)}"></label>
            <label>首抽 R1 <input type="number" min="0.01" max="1" step="0.01" name="R1" value="${Number(config.R1 ?? state.qcConfig.R1).toFixed(2)}"></label>
            <label>复抽 R2 <input type="number" min="0.01" max="1" step="0.01" name="R2" value="${Number(config.R2 ?? state.qcConfig.R2).toFixed(2)}"></label>
            <button type="submit">应用配置</button>
          </form>
          <table class="qc-stats-table">
            <thead><tr><th>作业人</th><th>已查/总量</th><th>不通过</th><th>正确率</th><th>当前阶段</th><th>状态</th><th>S1/S2配额</th><th>距阶段完成</th></tr></thead>
            <tbody>${rowsHtml || `<tr><td colspan="8" class="empty">暂无数据</td></tr>`}</tbody>
          </table>
        `;
        const btn = $("qcReworkBtn");
        if (btn) btn.addEventListener("click", runQcRework);
        const configForm = $("qcConfigForm");
        if (configForm) configForm.addEventListener("submit", saveQcConfig);
      } catch (error) {
        container.innerHTML = `<div class="empty">质检统计失败：${escapeHtml(error.message)}</div>`;
      }
    }

    async function saveQcConfig(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const cfg = {
        T: Number(form.elements.T.value),
        R1: Number(form.elements.R1.value),
        R2: Number(form.elements.R2.value)
      };
      if (![cfg.T, cfg.R1, cfg.R2].every(Number.isFinite)) {
        toast("质检配置必须是数字");
        return;
      }
      try {
        const response = await fetch("/api/qc-config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cfg)
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "配置保存失败");
        state.qcConfig = payload.config || cfg;
        localStorage.setItem("qcConfig", JSON.stringify(state.qcConfig));
        toast("质检配置已应用");
        renderQcStats();
      } catch (error) {
        toast(`配置保存失败：${error.message}`);
      }
    }

    async function runQcRework() {
      if (!confirm("确认将可疑范围内未质检条目打回返工？已质检结论会保留为事实记录。")) return;
      try {
        const response = await fetch("/api/qc-rework", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sourceUrl: activeSource?.url || "", sourceSheetId: activeSource?.sheetId || "", config: state.qcConfig })
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "返工失败");
        toast(`已打回 ${payload.count || 0} 条返工`);
        await loadRowsFromLark();
        renderQcStats();
      } catch (error) {
        toast(`返工失败：${error.message}`);
      }
    }

    function showDetail(index) {
      if (index < 0 || index >= state.filtered.length) return;
      if ($("listView").classList.contains("active")) rememberListPosition(state.filtered[index]);
      state.currentIndex = index;
      $("listView").classList.remove("active");
      $("distributionView").classList.remove("active");
      $("detailView").classList.add("active");
      document.body.classList.add("detail-mode");
      setDetailHash(state.filtered[index]);
      renderDetail();
      window.scrollTo({ top: 0, behavior: "auto" });
    }

    function setDetailHash(row) {
      if (!row) return;
      history.replaceState(null, "", `#detail=${encodeURIComponent(row.id)}`);
    }

    function clearDetailHash() {
      if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    }

    function restoreDetailFromHash() {
      const match = location.hash.match(/detail=([^&]+)/);
      if (!match) return;
      const id = decodeURIComponent(match[1]);
      const index = state.filtered.findIndex((row) => row.id === id);
      if (index >= 0) showDetail(index);
    }

    function rememberListPosition(row) {
      state.listScrollTop = window.scrollY || document.documentElement.scrollTop || 0;
      state.listAnchorId = row?.id || "";
      const anchor = state.listAnchorId
        ? document.querySelector(`[data-id="${CSS.escape(state.listAnchorId)}"]`)
        : null;
      state.listAnchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
    }

    function restoreListPosition() {
      window.requestAnimationFrame(() => {
        const anchor = state.listAnchorId
          ? document.querySelector(`[data-id="${CSS.escape(state.listAnchorId)}"]`)
          : null;
        if (anchor) {
          const targetTop = window.scrollY + anchor.getBoundingClientRect().top - state.listAnchorTop;
          window.scrollTo({ top: Math.max(0, targetTop), behavior: "auto" });
          return;
        }
        window.scrollTo({ top: state.listScrollTop || 0, behavior: "auto" });
      });
    }

    function renderDetail() {
      const row = currentRow();
      const promptIdText = String(row.promptId || "").trim();
      $("detailTitle").textContent = `${promptIdText ? `prompt_id：${promptIdText} · ` : ""}${state.currentIndex + 1} / ${state.filtered.length}`;
      $("originIndex").textContent = row.id;

      $("prevBtn").disabled = state.currentIndex === 0;
      $("nextBtn").disabled = state.currentIndex === state.filtered.length - 1;
      $("sidePrevBtn").disabled = state.currentIndex === 0;
      $("sideNextBtn").disabled = state.currentIndex === state.filtered.length - 1;

      if (row.mode === "ab-eval") {
        renderAbEvalDetail(row);
        $("detailMetaTags").innerHTML = renderAbEvalMeta(row);
        closeAnnotationPopover();
        return;
      }

      renderContent("originContent", row.c);
      renderPromptComparison(row);
      $("detailMetaTags").innerHTML = renderTagChips(row);
      renderIssuePanel(row);
      closeAnnotationPopover();
    }

    function cellImageUrl(token, spec = "big") {
      return `/api/cell-image?token=${encodeURIComponent(token)}&spec=${encodeURIComponent(spec)}`;
    }

    function renderAbImages(container, tokens, fallbackText) {
      if (!container) return;
      const list = Array.isArray(tokens) ? tokens : [];
      if (list.length) {
        container.innerHTML = `
          <div class="media-grid">
            ${list.map((token) => `<img src="${escapeAttr(cellImageUrl(token))}" alt="模型结果图，点击放大查看" loading="lazy" decoding="async" title="点击放大查看" data-image-token="${escapeAttr(token)}" onerror="this.alt='图片加载失败'; this.style.minHeight='120px';">`).join("")}
          </div>
        `;
        return;
      }
      renderContentElement(container, fallbackText);
    }

    function renderAbEvalDetail(row) {
      renderContent("originContent", row.c || "");
      renderAbEvalDynamicRelation(row);

      const verdicts = row.verdictOptions || ["模型A", "模型B", "无法区分"];
      const criteriaHtml = (row.criteria || []).map((item) => `
        <div class="ab-criterion" data-column="${escapeAttr(item.column)}">
          <div class="ab-criterion-title">${escapeHtml(item.title)}</div>
          <div class="ab-verdict-group" role="radiogroup" aria-label="${escapeAttr(item.title)}">
            ${verdicts.map((opt) => `
              <button type="button" class="ab-verdict ${item.verdict === opt ? "active" : ""}" data-column="${escapeAttr(item.column)}" data-value="${escapeAttr(opt)}" role="radio" aria-checked="${item.verdict === opt}">${escapeHtml(opt)}</button>
            `).join("")}
          </div>
        </div>
      `).join("");

      $("issueGrid").innerHTML = `<div class="ab-eval-panel">${criteriaHtml}</div>`;

      $("issueGrid").querySelectorAll(".ab-verdict").forEach((btn) => {
        btn.addEventListener("click", () => onAbVerdictClick(row, btn));
      });
      $("compareGrid").querySelectorAll('[data-role="ab-remark"]').forEach((ta) => {
        ta.addEventListener("change", () => onAbRemarkChange(row, ta));
      });

      if (state.workMode === "qc" && state.supportsQC) {
        renderQcArea(row);
      } else if ($("qcPanelContainer")) {
        $("qcPanelContainer").innerHTML = "";
      }
    }

    function renderAbEvalMeta(row) {
      const chips = [];
      if (row.reviewer) chips.push(`<span class="chip">评测人：${escapeHtml(row.reviewer)}</span>`);
      if (row.qcVerdict) chips.push(`<span class="chip">质检：${escapeHtml(row.qcVerdict)}</span>`);
      if (row.qcReviewer) chips.push(`<span class="chip">质检人：${escapeHtml(row.qcReviewer)}</span>`);
      return chips.join("");
    }

    function abEvalRelationGroups(row) {
      const groups = Array.isArray(row.promptGroups) && row.promptGroups.length
        ? row.promptGroups
        : [
            { id: "modelA", label: "模型A", result: row.modelA, imageTokens: row.modelAImages, resultLabel: "模型A 作业" },
            { id: "modelB", label: "模型B", result: row.modelB, imageTokens: row.modelBImages, resultLabel: "模型B 作业" }
          ];
      return groups.map((group) => ({
        ...group,
        resultLabel: group.resultLabel || `${group.label || "模型"} 作业`,
        imageTokens: Array.isArray(group.imageTokens) ? group.imageTokens : []
      }));
    }

    function abRemarkForGroup(row, group) {
      if (group.id === "modelA") return { title: "评测备注-模型A", column: row.remarkColumnA || "", value: row.remarkA || "" };
      if (group.id === "modelB") return { title: "评测备注-模型B", column: row.remarkColumnB || "", value: row.remarkB || "" };
      return null;
    }

    function renderAbEvalDynamicRelation(row) {
      const groups = abEvalRelationGroups(row);
      $("compareGrid").innerHTML = groups.map((group) => {
        const remark = abRemarkForGroup(row, group);
        return `
          <div class="compare-col ab-eval-col" data-group-id="${escapeAttr(group.id)}">
            <div class="block ab-relation-block">
              <div class="block-title">
                <span>${escapeHtml(group.resultLabel)}</span>
                <span class="chip pe">主表作业 ↔ 模板</span>
              </div>
              <div class="content ab-result-content" data-role="ab-result" data-group-id="${escapeAttr(group.id)}"></div>
            </div>
            ${remark ? `
              <div class="ab-remark-inline">
                <span class="ab-remark-title">${escapeHtml(remark.title)}</span>
                <textarea data-role="ab-remark" data-column="${escapeAttr(remark.column)}" placeholder="填写${escapeAttr(group.label)}备注">${escapeHtml(remark.value)}</textarea>
              </div>
            ` : ""}
          </div>
        `;
      }).join("");

      groups.forEach((group) => {
        renderAbImages(
          $("compareGrid").querySelector(`[data-role="ab-result"][data-group-id="${CSS.escape(group.id)}"]`),
          group.imageTokens,
          group.result
        );
      });
    }

    function renderQcArea(row) {
      const container = $("qcPanelContainer");
      if (!container) return;

      const verdict = row.qcVerdict || "";
      const checkedInfo = row.qcChecked
        ? `<div class="qc-status-line">已检 · 质检人：${escapeHtml(row.qcReviewer || "-")} · ${escapeHtml(row.qcTime || "")}</div>`
        : `<div class="qc-status-line">未检</div>`;
      const panel = document.createElement("div");
      panel.className = "qc-area";
      panel.innerHTML = `
        <div class="qc-area-title">
          <span>质检作业</span>
          ${checkedInfo}
        </div>
        <div class="qc-verdict-group" role="radiogroup" aria-label="质检结论">
          <button type="button" class="qc-verdict ${verdict === QC_PASS ? "active" : ""}" data-value="${QC_PASS}">${QC_PASS}</button>
          <button type="button" class="qc-verdict fail ${verdict === QC_FAIL ? "active" : ""}" data-value="${QC_FAIL}">${QC_FAIL}</button>
        </div>
        <textarea class="qc-comment" placeholder="质检意见（可空）">${escapeHtml(row.qcComment || "")}</textarea>
        <div class="qc-actions">
          <button type="button" class="qc-save-next">保存并下一条</button>
        </div>
      `;
      container.innerHTML = "";
      container.appendChild(panel);

      let selectedVerdict = verdict;
      panel.querySelectorAll(".qc-verdict").forEach((btn) => {
        btn.addEventListener("click", () => {
          selectedVerdict = btn.dataset.value;
          panel.querySelectorAll(".qc-verdict").forEach((b) => b.classList.toggle("active", b === btn));
        });
      });
      panel.querySelector(".qc-save-next").addEventListener("click", async () => {
        if (!selectedVerdict) {
          toast("请先选择 通过 / 不通过");
          return;
        }
        const comment = panel.querySelector(".qc-comment").value;
        await saveQc(row, selectedVerdict, comment, { goNext: true });
      });
    }

    async function saveQc(row, verdict, comment, options = {}) {
      const saveButton = document.querySelector(".qc-save-next");
      if (saveButton?.disabled) return;
      if (saveButton) {
        saveButton.disabled = true;
        saveButton.textContent = "保存中...";
      }
      try {
        const response = await fetch("/api/qc", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            excelRow: row.excelRow,
            recordId: row.recordId || "",
            sourceUrl: activeSource?.url || "",
            sourceSheetId: activeSource?.sheetId || "",
            verdict,
            comment,
            reviewer: state.qcReviewer,
            config: state.qcConfig,
            clientQcTime: row.qcTime || "",
            clientQcReviewer: row.qcReviewer || ""
          })
        });
        const payload = await response.json();
        if (response.status === 409 || payload.conflict) {
          toast(payload.error || "该条已被其他人质检，请刷新后再操作");
          return;
        }
        if (!response.ok || !payload.ok) throw new Error(payload.error || "保存失败");
        if (payload.previousReviewer && payload.previousReviewer !== state.qcReviewer) {
          toast(`提示：该条已被 ${payload.previousReviewer} 于 ${payload.previousTime || ""} 检过，已覆盖`);
        } else {
          toast("已保存质检结论");
        }
        // 本地更新该行状态
        row.qcChecked = true;
        row.qcVerdict = verdict;
        row.qcComment = comment;
        row.qcReviewer = payload.qcReviewer || state.qcReviewer;
        row.qcTime = payload.qcTime || "";
        row.qcStage = payload.qcStage || "";
        state.qcStats = null;
        renderDetail();
        // 后端给出的该员最新状态决定下一条
        state.lastAnnotatorState = payload.annotator || null;
        if (options.goNext) goToNextSuggested(row);
      } catch (error) {
        toast(`保存失败：${error.message}`);
      } finally {
        if (saveButton) {
          saveButton.disabled = false;
          saveButton.textContent = "保存并下一条";
        }
      }
    }

    function goToNextSuggested(row) {
      const nextId = state.lastAnnotatorState?.nextSuggestedId;
      if (!nextId) {
        toast("该标注员已无需继续质检（或状态待刷新统计页）");
        return;
      }
      const index = state.filtered.findIndex((r) => (r.promptId || r.id) === nextId);
      if (index >= 0) {
        showDetail(index);
      } else {
        toast("下一条待检不在当前筛选内，请切到「未检」筛选");
      }
    }

    async function onAbVerdictClick(row, btn) {
      const column = btn.dataset.column;
      const value = btn.dataset.value;
      const group = btn.closest(".ab-verdict-group");
      const prev = group.querySelector(".ab-verdict.active");
      group.querySelectorAll(".ab-verdict").forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-checked", b === btn ? "true" : "false");
      });
      try {
        await writeAbEval(row, column, value);
        const item = (row.criteria || []).find((c) => c.column === column);
        if (item) item.verdict = value;
        toast("已保存评测结论");
      } catch (error) {
        if (prev) {
          prev.classList.add("active");
          prev.setAttribute("aria-checked", "true");
        }
        btn.classList.remove("active");
        btn.setAttribute("aria-checked", "false");
        toast(`保存失败：${error.message}`);
      }
    }

    async function onAbRemarkChange(row, textarea) {
      const column = textarea.dataset.column;
      const value = textarea.value;
      try {
        await writeAbEval(row, column, value);
        if (column === row.remarkColumnA) row.remarkA = value;
        if (column === row.remarkColumnB) row.remarkB = value;
        toast("已保存备注");
      } catch (error) {
        toast(`保存失败：${error.message}`);
      }
    }

    async function writeAbEval(row, column, value) {
      const response = await fetch("/api/ab-eval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          excelRow: row.excelRow,
          recordId: row.recordId || "",
          sourceUrl: activeSource?.url || "",
          sourceSheetId: activeSource?.sheetId || "",
          column,
          value,
          operator: state.qcReviewer || ""
        })
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "写入失败");
      return payload.value;
    }

    function promptGroups(row) {
      if (Array.isArray(row.promptGroups) && row.promptGroups.length) return row.promptGroups;
      return [
        { id: "prompt-0", label: "PE Prompt", prompt: row.d, result: row.j, annotations: row.r, annotationColumn: "R", annotatable: true },
        { id: "prompt-1", label: "人工 Prompt", prompt: row.e, result: row.i, annotations: "", annotationColumn: "", annotatable: false }
      ].filter((group) => String(group.prompt || group.result || "").trim());
    }

    function promptGroupById(row, groupId) {
      return promptGroups(row).find((group) => group.id === groupId) || promptGroups(row)[0];
    }

    function promptSelectionKey() {
      return `pePromptSelection:${activeSource?.url || "default"}`;
    }

    function selectedPromptIdsFromStorage() {
      try {
        const saved = JSON.parse(localStorage.getItem(promptSelectionKey()) || "[]");
        return Array.isArray(saved) ? saved : [];
      } catch {
        return [];
      }
    }

    function saveSelectedPromptIds() {
      localStorage.setItem(promptSelectionKey(), JSON.stringify(state.selectedPromptGroupIds));
    }

    function normalizeSelectedPromptIds(row) {
      const groups = promptGroups(row);
      const validIds = new Set(groups.map((group) => group.id));
      let selected = state.selectedPromptGroupIds.length ? state.selectedPromptGroupIds : selectedPromptIdsFromStorage();
      selected = selected.filter((id, index) => validIds.has(id) && selected.indexOf(id) === index).slice(0, 3);
      if (!selected.length) {
        const annotatable = groups.filter((group) => group.annotatable).slice(0, 2);
        const readonly = groups.filter((group) => !group.annotatable).slice(0, 1);
        selected = [...annotatable, ...readonly].map((group) => group.id);
      }
      groups.forEach((group) => {
        if (selected.length < 3 && !selected.includes(group.id)) selected.push(group.id);
      });
      state.selectedPromptGroupIds = selected;
      saveSelectedPromptIds();
      return selected;
    }

    function visiblePromptGroups(row) {
      const groups = promptGroups(row);
      const selectedIds = normalizeSelectedPromptIds(row);
      return selectedIds.map((id) => groups.find((group) => group.id === id)).filter(Boolean).slice(0, 3);
    }

    function issueGroupBase(group) {
      return String(group.summaryTitle || group.labelTitle || "").replace(/-(问题(总结|分析|标签)|结果总结)$/i, "");
    }

    function normalizePromptBase(value) {
      return String(value || "")
        .replace(/-(问题(总结|分析|标签)|结果总结)$/i, "")
        .replace(/\s+/g, "")
        .toLowerCase();
    }

    function visibleIssueGroups(row) {
      const allIssueGroups = issueGroups(row);
      if (!Array.isArray(row.issueGroups) || !row.issueGroups.length) return allIssueGroups;
      const byBase = new Map(allIssueGroups.map((group) => [normalizePromptBase(issueGroupBase(group)), group]));
      const matched = visiblePromptGroups(row)
        .map((group) => byBase.get(normalizePromptBase(group.label)))
        .filter(Boolean);
      return matched;
    }

    function renderPromptTitle(row, group, slot) {
      const selectedIds = normalizeSelectedPromptIds(row);
      const options = promptGroups(row);
      const adjustmentTitle = String(group.adjustmentFlag || "").trim();
      return `
        <span class="prompt-title-wrap">
          <select class="prompt-title-select" data-prompt-slot="${slot}" aria-label="选择展示模型">
            ${options.map((option) => {
              const selectedElsewhere = selectedIds.includes(option.id) && option.id !== group.id;
              return `<option value="${escapeAttr(option.id)}" ${option.id === group.id ? "selected" : ""} ${selectedElsewhere ? "disabled" : ""}>${escapeHtml(option.label)}</option>`;
            }).join("")}
          </select>
          ${hasAdjustmentFlag(group.adjustmentFlag) ? `<span class="adjustment-dot" title="${escapeAttr(adjustmentTitle ? `有调整标识：${adjustmentTitle}` : "有调整标识")}"></span>` : ""}
        </span>
      `;
    }

    function renderPromptAnnotationStatus(row, group) {
      if (!group.annotatable) return `<span class="chip human">只读</span>`;
      const status = annotationStatusForPair(issueGroupForPromptGroup(row, group), group);
      const statusClass = status === "机标未校验" ? "unchecked" : status === "已标注" ? "checked" : "empty";
      return `
        <span class="prompt-status ${statusClass}">
          <span>${escapeHtml(status)}</span>
          ${status === "机标未校验" ? `<button type="button" data-action="confirm-machine-check" data-group-id="${escapeAttr(group.id)}" title="确认机器标注并切换为已标注" aria-label="确认机器标注">✓</button>` : ""}
        </span>
      `;
    }

    function handlePromptGroupSelection(event) {
      const select = event.target.closest("select[data-prompt-slot]");
      if (!select) return;
      const slot = Number(select.dataset.promptSlot);
      const row = currentRow();
      if (!row || !Number.isInteger(slot)) return;
      const nextId = select.value;
      const selected = normalizeSelectedPromptIds(row).slice();
      selected.forEach((id, index) => {
        if (id === nextId && index !== slot) selected[index] = "";
      });
      selected[slot] = nextId;
      state.selectedPromptGroupIds = selected.filter(Boolean);
      saveSelectedPromptIds();
      renderPromptComparison(row);
      renderIssuePanel(row);
      renderList();
    }

    function renderPromptComparison(row) {
      $("compareGrid").innerHTML = visiblePromptGroups(row).map((group, slot) => `
        <div class="compare-col" data-group-id="${escapeAttr(group.id)}">
          <div class="block">
            <div class="block-title">
              ${renderPromptTitle(row, group, slot)}
              ${renderPromptAnnotationStatus(row, group)}
            </div>
            <div class="content prompt-content" data-role="prompt-content" data-group-id="${escapeAttr(group.id)}"></div>
          </div>
          <div class="block">
            <div class="block-title"><span>${escapeHtml(group.resultLabel || `${group.label}结果`)}</span><span class="chip ${group.annotatable ? "pe" : "human"}">结果</span></div>
            <div class="content result-content" data-role="result-content" data-group-id="${escapeAttr(group.id)}"></div>
          </div>
        </div>
      `).join("");

      visiblePromptGroups(row).forEach((group) => {
        renderAnnotatedPromptIn($(`compareGrid`).querySelector(`[data-role="prompt-content"][data-group-id="${CSS.escape(group.id)}"]`), row, group);
        renderContentElement($(`compareGrid`).querySelector(`[data-role="result-content"][data-group-id="${CSS.escape(group.id)}"]`), group.result);
      });
    }

    function renderListThumb(value) {
      const urls = extractUrls(value || "");
      if (!urls.length) return "";

      return `
        <span class="row-thumb">
          <img src="${escapeAttr(urls[0])}" alt="C 列关联图片" loading="lazy" decoding="async" onerror="this.parentElement.classList.add('empty-thumb'); this.parentElement.textContent='图片加载失败';">
        </span>
      `;
    }

    function renderAbEvalCard(row, index) {
      const verdictChips = (row.criteria || []).map((item) =>
        `<span class="chip">${escapeHtml(shortCriterionTitle(item.title))}：${escapeHtml(item.verdict || "未评")}</span>`
      ).join("");
      const groups = abEvalRelationGroups(row);
      return `
        <article class="waterfall-card" data-id="${escapeAttr(row.id)}" aria-label="详细浏览项，点击进入详情页" title="点击进入详情页">
          <div class="waterfall-head">
            <span class="index">${index}</span>
            ${renderAbEvalRowChips(row)}
          </div>
          <div class="waterfall-body">
            <section class="mini-section">
              <span class="mini-title">prompt_cn</span>
              ${renderMiniText(row.c, "prompt-cn")}
            </section>
            <div class="detail-preview-grid">
              ${groups.map((group) => `
                <div class="detail-preview-col">
                  <section class="mini-section">
                    <span class="mini-title">${escapeHtml(group.resultLabel || group.label || "主表作业")}</span>
                    ${renderAbMiniImages(group.imageTokens)}
                  </section>
                </div>
              `).join("")}
            </div>
            <div class="detail-preview-footer">
              <section class="mini-section">
                <span class="mini-title">考点结论</span>
                <div class="tag-chips">${verdictChips || '<span class="mini-empty">未评测</span>'}</div>
              </section>
            </div>
          </div>
        </article>
      `;
    }

    function renderAbEvalRowChips(row) {
      const chips = [];
      if (row.reviewer) chips.push(`<span class="chip">评测人：${escapeHtml(row.reviewer)}</span>`);
      if (row.qcVerdict) chips.push(`<span class="chip">质检：${escapeHtml(row.qcVerdict)}</span>`);
      if (!chips.length) return `<span class="tag-chips"><span class="empty">无作业状态</span></span>`;
      return `<span class="tag-chips">${chips.join("")}</span>`;
    }

    function shortCriterionTitle(title) {
      const match = String(title || "").match(/^考点\d+/);
      return match ? match[0] : title;
    }

    function renderAbMiniImages(tokens) {
      const list = Array.isArray(tokens) ? tokens : [];
      if (!list.length) return `<div class="mini-empty">无图片</div>`;
      return list.map((token) => `
        <img class="mini-image" src="${escapeAttr(cellImageUrl(token, "middle"))}" alt="模型结果图，点击放大查看" loading="lazy" decoding="async" title="点击放大查看" data-image-token="${escapeAttr(token)}" onerror="this.replaceWith(Object.assign(document.createElement('div'), { className: 'mini-empty', textContent: '图片加载失败' }))">
      `).join("");
    }

    function renderWaterfallCard(row, index) {
      return `
        <article class="waterfall-card" data-id="${escapeAttr(row.id)}" aria-label="详细浏览项，点击进入详情页" title="点击进入详情页">
          <div class="waterfall-head">
            <span class="index">${index}</span>
            ${renderTagChips(row, 6)}
          </div>
          <div class="waterfall-body">
            <section class="mini-section">
              <span class="mini-title">prompt_cn</span>
              ${renderMiniText(row.c, "prompt-cn")}
              ${renderMiniImages(row.c, "prompt_cn 关联图片")}
            </section>
            <div class="detail-preview-grid">
              ${renderDetailPreviewColumns(row)}
            </div>
            <div class="detail-preview-footer">
              <section class="mini-section">
                <span class="mini-title">标签</span>
                ${renderTagChips(row, 8)}
              </section>
              ${renderMiniIssueGroups(row)}
            </div>
          </div>
        </article>
      `;
    }

    function renderDetailPreviewColumns(row) {
      return visiblePromptGroups(row).map((group) => `
        <div class="detail-preview-col">
          <section class="mini-section">
            <span class="mini-title">${escapeHtml(group.label)}</span>
            ${renderMiniPromptGroup(row, group)}
          </section>
          <section class="mini-section">
            <span class="mini-title">${escapeHtml(group.resultLabel || `${group.label}结果`)}</span>
            ${renderMiniImages(group.result, group.resultLabel || `${group.label}结果`)}
          </section>
        </div>
      `).join("");
    }

    function renderMiniIssueGroups(row) {
      return visibleIssueGroups(row).map((group) => `
        <section class="mini-section">
          <span class="mini-title">${escapeHtml(issueAnalysisTitle(group))}</span>
          ${renderMiniIssueLabels(group.imageLabels, "图像效果标签")}
          ${renderMiniIssueLabels(group.labels, "PE问题标签")}
          ${renderMiniSummary(group.summary)}
        </section>
      `).join("");
    }

    function renderMiniIssueLabels(value, title = "PE问题标签") {
      const labels = problemTags(value);
      const content = labels.length
        ? `<span class="tag-chips">${labels.map((label) => `<span class="tag-chip">${escapeHtml(label)}</span>`).join("")}</span>`
        : `<span class="mini-empty compact">无</span>`;
      return `
        <div class="mini-issue-row">
          <span class="mini-issue-label">${escapeHtml(title)}：</span>
          <span class="mini-issue-value">${content}</span>
        </div>
      `;
    }

    function renderMiniSummary(value) {
      const text = stripUrls(value || "").trim();
      return `
        <div class="mini-issue-row">
          <span class="mini-issue-label">问题总结：</span>
          <span class="mini-issue-value">${text ? escapeHtml(text) : `<span class="mini-empty compact">无</span>`}</span>
        </div>
      `;
    }

    function renderMiniText(value, extraClass = "") {
      const text = stripUrls(value || "").trim();
      return text
        ? `<div class="mini-text ${escapeAttr(extraClass)}">${escapeHtml(text)}</div>`
        : `<div class="mini-empty">无文本内容</div>`;
    }

    function renderMiniAnnotatedPrompt(row) {
      const group = visiblePromptGroups(row).find((item) => item.annotatable) || visiblePromptGroups(row)[0];
      return renderMiniPromptGroup(row, group);
    }

    function renderMiniPromptGroup(row, group) {
      const text = promptText(row, group?.id);
      if (!text.trim()) return `<div class="mini-empty">无文本内容</div>`;
      const annotations = group?.annotatable
        ? annotationsForRow(row, group.id).filter((item) => item.start >= 0 && item.end <= text.length)
        : [];
      let cursor = 0;
      const html = annotations.map((item) => {
        const before = escapeHtml(text.slice(cursor, item.start));
        const marked = escapeHtml(text.slice(item.start, item.end));
        cursor = item.end;
        return `${before}<span class="pe-mark ${annotationSourceClass(item)}" title="${escapeAttr(item.reason || "无错误理由")}">${marked}</span>`;
      }).join("") + escapeHtml(text.slice(cursor));
      return `<div class="mini-text">${html}</div>`;
    }

    function renderMiniImages(value, alt) {
      const urls = extractUrls(value || "");
      if (!urls.length) return `<div class="mini-empty">无图片</div>`;
      return urls.map((url) => `
        <img class="mini-image" src="${escapeAttr(url)}" alt="${escapeAttr(alt)}，点击放大查看" loading="lazy" decoding="async" title="点击放大查看" onerror="this.replaceWith(Object.assign(document.createElement('div'), { className: 'mini-empty', textContent: '图片加载失败' }))">
      `).join("");
    }

    function imageMeta(value) {
      const count = extractUrls(value || "").length;
      return count ? `C 列包含 ${count} 张图片信息` : "C 列无图片信息";
    }

    function parseTags(value) {
      const byKey = new Map();
      String(value || "").split("；").forEach((part) => {
        const [rawKey, ...rest] = part.split("：");
        const key = (rawKey || "").trim();
        const tagValue = rest.join("：").trim();
        if (!tagKeys.includes(key) || !tagValue) return;
        if (!byKey.has(key)) byKey.set(key, new Set());
        byKey.get(key).add(tagValue);
      });

      return tagKeys.flatMap((key) => {
        const values = Array.from(byKey.get(key) || []);
        return values.map((value) => ({ key, value, label: `${key}：${value}` }));
      });
    }

    function rowTagLabels(row) {
      return parseTags(row.f).map((tag) => tag.label);
    }

    function issueGroupLabel(group) {
      const title = group.summaryTitle || group.labelTitle || "问题标签";
      return title.replace(/-问题(总结|标签|分析)$/i, "");
    }

    function issueModelOptions() {
      // 性能优化：使用缓存
      const rowsHash = getRowsHash();
      if (cache.issueModelOptions && cache.lastRowsHash === rowsHash) {
        return cache.issueModelOptions;
      }

      if (rows.some((row) => row.mode === "ab-eval")) {
        const result = [{ value: allIssueModelsValue, label: "全部作业" }];
        cache.issueModelOptions = result;
        cache.lastRowsHash = rowsHash;
        return result;
      }

      const models = [];
      const seenModelIds = new Set();
      rows.forEach((row) => {
        issueGroups(row).forEach((group) => {
          if (!seenModelIds.has(group.id)) {
            seenModelIds.add(group.id);
            models.push({ value: group.id, label: issueGroupLabel(group) });
          }
        });
      });
      const result = [{ value: allIssueModelsValue, label: "全部模型" }, ...models];
      cache.issueModelOptions = result;
      cache.lastRowsHash = rowsHash;
      return result;
    }

    function issueLabelOptions(modelId = state.issueModel) {
      // 性能优化：使用缓存
      const rowsHash = getRowsHash();
      const cacheKey = `${modelId}-${rowsHash}`;
      if (cache.issueLabelOptions.has(cacheKey)) {
        return cache.issueLabelOptions.get(cacheKey);
      }

      if (rows.some((row) => row.mode === "ab-eval")) {
        const result = [{ value: allIssueLabelsValue, label: "全部结论" }];
        cache.issueLabelOptions.set(cacheKey, result);
        return result;
      }

      const labels = new Set();
      rows.forEach((row) => {
        issueGroups(row).forEach((group) => {
          if (modelId !== allIssueModelsValue && group.id !== modelId) return;
          problemTags(group.labels).forEach((label) => labels.add(label));
        });
      });
      const result = [
        { value: allIssueLabelsValue, label: "全部问题标签" },
        ...Array.from(labels).sort((a, b) => a.localeCompare(b, "zh-CN")).map((label) => ({ value: label, label }))
      ];
      cache.issueLabelOptions.set(cacheKey, result);
      return result;
    }

    function issueTagFilterMatches(row) {
      if (row.mode === "ab-eval") return true;
      if (state.issueModel === allIssueModelsValue && state.issueLabel === allIssueLabelsValue) return true;
      if (state.issueLabel === allIssueLabelsValue) return true;
      return issueGroups(row).some((group) => {
        if (state.issueModel !== allIssueModelsValue && group.id !== state.issueModel) return false;
        return problemTags(group.labels).includes(state.issueLabel);
      });
    }

    function hasAdjustmentFlag(value) {
      const text = String(value || "").trim();
      if (!text) return false;
      return !["无", "否", "0", "false", "no", "none"].includes(text.toLowerCase());
    }

    // 调整标识筛选：全部模型时任一模型命中即可；指定模型时按问题组反查对应 prompt 组。
    function adjustmentFlagForRow(row) {
      if (row.mode === "ab-eval") return false;
      if (state.issueModel === allIssueModelsValue) {
        return promptGroups(row).some((group) => hasAdjustmentFlag(group.adjustmentFlag));
      }
      const issueGroup = issueGroups(row).find((group) => group.id === state.issueModel);
      const promptGroup = promptGroupForIssueGroup(row, issueGroup);
      return hasAdjustmentFlag(promptGroup?.adjustmentFlag);
    }

    function adjustmentFlagMatches(row) {
      if (state.adjustmentFlag === "全部") return true;
      const hasFlag = adjustmentFlagForRow(row);
      return state.adjustmentFlag === "有调整" ? hasFlag : !hasFlag;
    }

    function rowMatchesFilters(row, { includeIssueFilter = true } = {}) {
      const abMode = row.mode === "ab-eval";
      const matchesAnnotationStatus = abMode || state.annotationStatus === "全部" || annotationStatusForRow(row) === state.annotationStatus;
      const matchesAdjustmentFlag = abMode || adjustmentFlagMatches(row);
      const matchesTag = abMode || state.tag === "全部标签" || rowTagLabels(row).includes(state.tag);
      const matchesIssueTag = !includeIssueFilter || issueTagFilterMatches(row);
      const matchesQuery = keywordMatches(row, state.query);
      const matchesQcStatus = !(state.workMode === "qc" && state.supportsQC) || state.qcStatus === "全部" || qcStatusForRow(row) === state.qcStatus;
      return matchesAnnotationStatus && matchesAdjustmentFlag && matchesTag && matchesIssueTag && matchesQuery && matchesQcStatus;
    }

    function issueDistributionRows() {
      return rows.filter((row) => row.mode !== "ab-eval" && rowMatchesFilters(row, { includeIssueFilter: false }));
    }

    function issueDistributionStats() {
      // 性能优化：使用缓存
      const rowsHash = getRowsHash();
      // 缓存 key 需要包含筛选状态，因为 issueDistributionRows 依赖筛选
      const cacheKey = `${rowsHash}-${JSON.stringify({
        annotationStatus: state.annotationStatus,
        tag: state.tag,
        adjustmentFlag: state.adjustmentFlag
      })}`;

      if (cache.issueDistributionStats && cache.issueDistributionStats.key === cacheKey) {
        return cache.issueDistributionStats.value;
      }

      const groups = new Map();
      // 性能优化：预计算每个模型的标签，用于计算重叠
      const modelLabelSets = new Map(); // modelId -> Map(label -> Set(rowId))

      issueDistributionRows().forEach((row) => {
        issueGroups(row).forEach((group) => {
          if (!groups.has(group.id)) {
            groups.set(group.id, { id: group.id, label: issueGroupLabel(group), tags: new Map(), annotatedTotal: 0, problemTotal: 0 });
            modelLabelSets.set(group.id, new Map());
          }
          const stat = groups.get(group.id);
          const labelSet = modelLabelSets.get(group.id);

          const labels = problemTags(group.labels);
          const imageLabels = problemTags(group.imageLabels);
          const hasSummary = String(group.summary || "").trim().length > 0;
          if (labels.length || imageLabels.length || hasSummary) stat.annotatedTotal += 1;
          if (labels.length) stat.problemTotal += 1;

          labels.forEach((label) => {
            stat.tags.set(label, (stat.tags.get(label) || 0) + 1);
            // 记录哪些行有这个标签
            if (!labelSet.has(label)) labelSet.set(label, new Set());
            labelSet.get(label).add(row.id);
          });
        });
      });

      const result = Array.from(groups.values()).map((group) => {
        const tags = Array.from(group.tags.entries())
          .map(([label, count]) => ({ label, count }))
          .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, "zh-CN"));
        return {
          ...group,
          tags,
          tagTotal: tags.reduce((sum, item) => sum + item.count, 0),
          maxCount: tags[0]?.count || 0,
          labelRows: modelLabelSets.get(group.id) // 保留标签的行信息
        };
      });

      cache.issueDistributionStats = { key: cacheKey, value: result };
      return result;
    }

    // 性能优化：使用预计算的信息计算重叠
    function issueOverlapCount(label, leftGroupId, rightGroupId) {
      const stats = issueDistributionStats();
      const leftStat = stats.find(s => s.id === leftGroupId);
      const rightStat = stats.find(s => s.id === rightGroupId);
      if (!leftStat?.labelRows || !rightStat?.labelRows) return 0;

      const leftRows = leftStat.labelRows.get(label);
      const rightRows = rightStat.labelRows.get(label);
      if (!leftRows || !rightRows) return 0;

      // 计算交集大小
      let overlap = 0;
      const [small, large] = leftRows.size < rightRows.size ? [leftRows, rightRows] : [rightRows, leftRows];
      small.forEach(id => {
        if (large.has(id)) overlap++;
      });
      return overlap;
    }

    function orderedIssueDistributionStats() {
      const stats = issueDistributionStats();
      if (!state.distributionOrder.length || state.distributionOrder.some((id) => !stats.some((group) => group.id === id))) {
        state.distributionOrder = stats.map((group) => group.id);
      }
      const byId = new Map(stats.map((group) => [group.id, group]));
      return [
        ...state.distributionOrder.map((id) => byId.get(id)).filter(Boolean),
        ...stats.filter((group) => !state.distributionOrder.includes(group.id))
      ];
    }

    function syncDistributionModelSelection(stats) {
      const ids = stats.map((group) => group.id);
      if (!ids.length) {
        state.distributionLeftModel = "";
        state.distributionRightModel = "";
        return [];
      }
      if (!ids.includes(state.distributionLeftModel)) state.distributionLeftModel = ids[0] || "";
      if (!ids.includes(state.distributionRightModel) || state.distributionRightModel === state.distributionLeftModel) {
        state.distributionRightModel = ids.find((id) => id !== state.distributionLeftModel) || "";
      }
      return [state.distributionLeftModel, state.distributionRightModel].filter(Boolean);
    }

    function renderDistributionModelPickers(stats) {
      const options = stats.map((group) => `
        <option value="${escapeAttr(group.id)}">${escapeHtml(group.label)}</option>
      `).join("");
      $("distributionLeftModel").innerHTML = options;
      $("distributionRightModel").innerHTML = options;
      $("distributionLeftModel").value = state.distributionLeftModel;
      $("distributionRightModel").value = state.distributionRightModel;
      $("distributionLeftModel").disabled = stats.length < 2;
      $("distributionRightModel").disabled = stats.length < 2;
      $("distributionSwapBtn").disabled = stats.length < 2;
    }

    function changeLabel(value) {
      if (!Number.isFinite(value)) return "—";
      if (value === 0) return "持平";
      return `${value > 0 ? "增加" : "减少"} ${Math.abs(value).toFixed(1)}%`;
    }

    function changeClass(value) {
      if (value === Infinity) return "up";
      if (!Number.isFinite(value) || value === 0) return "flat";
      return value > 0 ? "up" : "down";
    }

    function renderCompareMeter(count, maxCount, side, commonCount) {
      const totalWidth = Math.max(count ? 4 : 0, Math.round((count / Math.max(1, maxCount)) * 100));
      const commonWidth = Math.max(commonCount ? 3 : 0, Math.round((commonCount / Math.max(1, maxCount)) * 100));
      return `
        <span class="compare-meter ${escapeAttr(side)}" title="同一道题均有该问题标签的题目数是：${commonCount}">
          <span class="compare-meter-count">
            <strong>${count}</strong>
          </span>
          <span class="compare-meter-track">
            <span class="compare-meter-fill" style="width: ${totalWidth}%"></span>
            <span class="compare-meter-common" style="width: ${Math.min(commonWidth, totalWidth)}%"></span>
          </span>
        </span>
      `;
    }

    function renderCompareModelHead(group) {
      return `
        <span class="compare-model-head">
          <span class="compare-model-name" title="${escapeAttr(group.label)}">${escapeHtml(group.label)}</span>
          <span class="compare-model-meta">
            <span>已标注 ${group.annotatedTotal}</span>
            <span>有问题 ${group.problemTotal}</span>
          </span>
        </span>
      `;
    }

    function renderIssueCompareChart(stats, selectedIds) {
      if (stats.length < 2 || selectedIds.length < 2) {
        $("issueCompareChart").innerHTML = `<span class="empty">至少需要两个模型的问题标签数据，才能展示对比。</span>`;
        return;
      }
      const byId = new Map(stats.map((group) => [group.id, group]));
      const left = byId.get(selectedIds[0]);
      const right = byId.get(selectedIds[1]);
      if (!left || !right) {
        $("issueCompareChart").innerHTML = `<span class="empty">请选择两个有效模型进行对比。</span>`;
        return;
      }
      const leftCounts = new Map(left.tags.map((tag) => [tag.label, tag.count]));
      const rightCounts = new Map(right.tags.map((tag) => [tag.label, tag.count]));
      const labels = Array.from(new Set([...leftCounts.keys(), ...rightCounts.keys()]))
        .map((label) => ({
          label,
          left: leftCounts.get(label) || 0,
          right: rightCounts.get(label) || 0
        }))
        .sort((a, b) => (b.left + b.right) - (a.left + a.right) || a.label.localeCompare(b.label, "zh-CN"));
      const maxCount = Math.max(1, ...labels.flatMap((item) => [item.left, item.right]));
      if (!labels.length) {
        $("issueCompareChart").innerHTML = `
          <div class="issue-dist-title">模型问题标签对比</div>
          <div class="distribution-total">当前筛选条件下暂无问题标签；仍展示两个模型的标注状态。</div>
          <div class="compare-chart-head">
            <span>问题标签</span>
            ${renderCompareModelHead(left)}
            ${renderCompareModelHead(right)}
            <span>变化</span>
          </div>
          <span class="empty">暂无问题标签数据</span>
        `;
        return;
      }

      $("issueCompareChart").innerHTML = `
        <div class="issue-dist-title">模型问题标签对比</div>
        <div class="distribution-total">比较方向：${escapeHtml(left.label)} → ${escapeHtml(right.label)}；深色叠加段表示两个模型在同一条样本上都有该错误的数量。</div>
        <div class="compare-chart-head">
          <span>问题标签</span>
          ${renderCompareModelHead(left)}
          ${renderCompareModelHead(right)}
          <span>变化</span>
        </div>
        ${labels.map((item) => {
          const change = item.left === 0 ? (item.right === 0 ? 0 : Infinity) : ((item.right - item.left) / item.left) * 100;
          const common = issueOverlapCount(item.label, left.id, right.id);
          return `
            <button type="button" class="compare-chart-row" data-issue-compare-tag="${escapeAttr(item.label)}" data-issue-compare-model="${escapeAttr(left.id)}" title="查看「${escapeAttr(left.label)}」中打上「${escapeAttr(item.label)}」的列表">
              <span class="compare-chart-label">${escapeHtml(item.label)}</span>
              ${renderCompareMeter(item.left, maxCount, "left", common)}
              ${renderCompareMeter(item.right, maxCount, "right", common)}
              <span class="change-pill ${changeClass(change)}">${change === Infinity ? "新增" : changeLabel(change)}</span>
            </button>
          `;
        }).join("")}
      `;
    }

    function renderIssueDistribution() {
      const stats = orderedIssueDistributionStats();
      $("distributionSummary").textContent = ` · 基于当前筛选条件，统计 ${issueDistributionRows().length} 条候选数据`;
      const selectedIds = syncDistributionModelSelection(stats);
      renderDistributionModelPickers(stats);
      if (!stats.length) {
        $("issueCompareChart").innerHTML = `<span class="empty">暂无问题标签分布</span>`;
        return;
      }
      renderIssueCompareChart(stats, selectedIds);
    }

    function issueFilterSummary() {
      if (state.issueModel === allIssueModelsValue && state.issueLabel === allIssueLabelsValue) return "";
      const modelLabel = issueModelOptions().find((item) => item.value === state.issueModel)?.label || "全部模型";
      const labelText = state.issueLabel === allIssueLabelsValue ? "任意问题标签" : state.issueLabel;
      return `，问题标签：${modelLabel} / ${labelText}`;
    }

    function renderTagChips(row, limit = Infinity) {
      if (row.mode === "ab-eval") return renderAbEvalRowChips(row);
      const tags = parseTags(row.f).slice(0, limit);
      if (!tags.length) return `<span class="tag-chips"><span class="empty">无标签信息</span></span>`;
      return `
        <span class="tag-chips">
          ${tags.map((tag) => `<span class="tag-chip" title="${escapeAttr(tag.label)}">${escapeHtml(tag.label)}</span>`).join("")}
        </span>
      `;
    }

    function getTagOptions() {
      // 性能优化：使用缓存
      const rowsHash = getRowsHash();
      if (cache.tagOptions && cache.lastRowsHash === rowsHash) {
        return cache.tagOptions;
      }

      const options = new Set(["全部标签"]);
      rows.forEach((row) => {
        if (row.mode === "ab-eval") return;
        rowTagLabels(row).forEach((label) => options.add(label));
      });
      const result = Array.from(options);
      cache.tagOptions = result;
      cache.lastRowsHash = rowsHash;
      return result;
    }

    function renderPagination(pages) {
      $("pagination").innerHTML = `
        <button type="button" id="firstPageBtn" ${state.page === 1 ? "disabled" : ""}>首页</button>
        <button type="button" id="prevPageBtn" ${state.page === 1 ? "disabled" : ""}>上一页</button>
        <span>第</span>
        <input class="page-jump" id="pageJump" type="number" min="1" max="${pages}" value="${state.page}" aria-label="页码">
        <span>页 / 共 ${pages} 页</span>
        <button type="button" id="nextPageBtn" ${state.page === pages ? "disabled" : ""}>下一页</button>
        <button type="button" id="lastPageBtn" ${state.page === pages ? "disabled" : ""}>末页</button>
      `;

      $("firstPageBtn").addEventListener("click", () => goPage(1));
      $("prevPageBtn").addEventListener("click", () => goPage(state.page - 1));
      $("nextPageBtn").addEventListener("click", () => goPage(state.page + 1));
      $("lastPageBtn").addEventListener("click", () => goPage(pages));
      $("pageJump").addEventListener("change", (event) => goPage(Number(event.target.value)));
    }

    function goPage(page) {
      state.page = Math.min(Math.max(1, page || 1), totalPages());
      renderList();
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function totalPages() {
      return Math.max(1, Math.ceil(state.filtered.length / pageSize));
    }

    function promptText(row, groupId = "") {
      const group = groupId ? promptGroupById(row, groupId) : promptGroups(row)[0];
      return String(group?.prompt || "");
    }

    // 解析某个 prompt 标注记录列的 JSON，返回划线备注数组；异常时按空数组处理。
    function parseAnnotationRecord(value, source = "manual") {
      return parseAnnotationRecordMeta(value, source).annotations;
    }

    function normalizeAnnotationItem(item, source = "manual") {
      const start = Number(item?.start);
      const end = Number(item?.end);
      if (!Number.isInteger(start) || !Number.isInteger(end) || end <= start) return null;
      const level = item.level === "serious" || item.level === "normal" ? item.level : "normal";
      return {
        ...item,
        id: String(item.id || `ann-${start}-${end}`),
        start,
        end,
        text: String(item.text || ""),
        level,
        source,
        reason: String(item.reason || item.comment || item.problem || "")
      };
    }

    // 解析标注记录完整状态：annotations 用于渲染，unchecked 用于识别机器标注待人工确认。
    function parseAnnotationRecordMeta(value, source = "manual") {
      try {
        const raw = String(value || "").trim();
        if (!raw) return { annotations: [], unchecked: false, checked: false, raw: "" };
        const parsed = JSON.parse(raw);
        const sourceAnnotations = Array.isArray(parsed)
          ? parsed
          : Array.isArray(parsed.annotations)
          ? parsed.annotations
          : (parsed && typeof parsed === "object" && parsed.start !== undefined && parsed.end !== undefined)
            ? [parsed]
            : [];
        const checks = Array.isArray(parsed) ? parsed.map((item) => item?.check) : [parsed?.check];
        const unchecked = checks.some(isUncheckedCheckValue);
        const checked = checks.some(isCheckedCheckValue) || parsed.checked === 1 || parsed.checked === true;
        const annotationSource = source === "machine" || unchecked ? "machine" : "manual";
        return {
          annotations: sourceAnnotations
            .map((item) => normalizeAnnotationItem(item, annotationSource))
            .filter(Boolean)
            .sort((a, b) => a.start - b.start),
          unchecked,
          checked,
          raw
        };
      } catch {
        return { annotations: [], unchecked: false, checked: false, raw: String(value || "").trim() };
      }
    }

    function serializeAnnotationRecord(annotations, meta = {}) {
      const record = { annotations };
      if (meta.checked) record.checked = 1;
      return JSON.stringify(record);
    }

    function annotationsForRow(row, groupId = "") {
      if (groupId) {
        const group = promptGroups(row).find((item) => item.id === groupId);
        return annotationRecordMetaForGroup(group).annotations;
      }
      const group = promptGroups(row)[0];
      const meta = annotationRecordMetaForGroup(group);
      return meta.annotations.length ? meta.annotations : parseAnnotationRecord(row.r || "");
    }

    function annotationRecordMetaForGroup(group) {
      const manual = String(group?.annotations || "").trim();
      return manual ? parseAnnotationRecordMeta(manual, "manual") : parseAnnotationRecordMeta(group?.machineAnnotations || "", "machine");
    }

    function manualAnnotationRecordMetaForGroup(group) {
      return parseAnnotationRecordMeta(group?.annotations || "", "manual");
    }

    function promptGroupForIssueGroup(row, issueGroup) {
      if (!issueGroup) return null;
      const base = normalizePromptBase(issueGroupBase(issueGroup));
      return promptGroups(row).find((group) => normalizePromptBase(group.label) === base) || null;
    }

    function issueGroupForPromptGroup(row, promptGroup) {
      if (!promptGroup) return null;
      const base = normalizePromptBase(promptGroup.label);
      return issueGroups(row).find((group) => normalizePromptBase(issueGroupBase(group)) === base) || null;
    }

    function groupHasIssueAnnotation(group) {
      const labelMeta = parseIssueLabelRecord(group?.labels);
      return labelMeta.labels.length > 0 || problemTags(group?.imageLabels).length > 0 || labelMeta.unchecked || labelMeta.checked || String(group?.summary || "").trim().length > 0;
    }

    function annotationStatusForPair(issueGroup, promptGroup) {
      const labelMeta = parseIssueLabelRecord(issueGroup?.labels);
      const annotationMeta = annotationRecordMetaForGroup(promptGroup);
      const manualAnnotationMeta = manualAnnotationRecordMetaForGroup(promptGroup);
      const hasIssue = labelMeta.labels.length > 0 || problemTags(issueGroup?.imageLabels).length > 0 || labelMeta.unchecked || labelMeta.checked || String(issueGroup?.summary || "").trim().length > 0;
      const hasLineAnnotations = annotationMeta.annotations.length > 0 || annotationMeta.unchecked || manualAnnotationMeta.checked;
      if (!hasIssue && !hasLineAnnotations) return "未标注";
      if (labelMeta.unchecked || annotationMeta.unchecked) return "机标未校验";
      return "已标注";
    }

    // 标注状态判定：全部模型按任一模型有问题标签/总结/划线即已标注；check:0 优先视为机标未校验。
    function annotationStatus(row) {
      const statuses = issueGroups(row).map((issueGroup) => annotationStatusForPair(issueGroup, promptGroupForIssueGroup(row, issueGroup)));
      promptGroups(row).forEach((promptGroup) => {
        if (issueGroupForPromptGroup(row, promptGroup)) return;
        statuses.push(annotationStatusForPair(null, promptGroup));
      });
      if (statuses.includes("机标未校验")) return "机标未校验";
      if (statuses.includes("已标注")) return "已标注";
      return "未标注";
    }

    function annotationStatusForRow(row) {
      if (row.mode === "ab-eval") return row.qcChecked ? "已标注" : "未标注";
      if (state.issueModel === allIssueModelsValue) return annotationStatus(row);
      const issueGroup = issueGroups(row).find((group) => group.id === state.issueModel);
      const promptGroup = promptGroupForIssueGroup(row, issueGroup);
      return annotationStatusForPair(issueGroup, promptGroup);
    }

    // PE 标注内容检索文本：P 列 + R 列每条 annotation 的 reason/text。
    function peAnnotationSearchText(row) {
      if (row.mode === "ab-eval") {
        return [
          row.reviewer || "",
          row.qcVerdict || "",
          row.qcComment || "",
          ...(row.criteria || []).flatMap((item) => [item.title || "", item.verdict || ""])
        ].join("\n");
      }
      const annotationText = promptGroups(row).flatMap((group) => annotationsForRow(row, group.id))
        .flatMap((item) => [item.reason || "", item.text || ""])
        .join("\n");
      const issueText = issueGroups(row).flatMap((group) => [group.imageLabels || "", group.labels || "", group.summary || ""]).join("\n");
      return `${row.p || ""}\n${issueText}\n${annotationText}`;
    }

    // 关键词匹配：大小写不敏感，按当前检索范围做普通子串匹配。
    function keywordMatches(row, query) {
      const keyword = String(query || "").trim().toLowerCase();
      if (!keyword) return true;
      const source = row.mode === "ab-eval"
        ? [row.c || "", row.promptId || "", peAnnotationSearchText(row)].join("\n")
        : (state.searchScope === "peAnnotation" ? peAnnotationSearchText(row) : row.c);
      return String(source || "").toLowerCase().includes(keyword);
    }

    // 按 start/end 偏移渲染指定 prompt 文本；只有可标注 prompt 容器响应划线交互。
    function renderAnnotatedPromptIn(container, row, group) {
      if (!container) return;
      const text = String(group.prompt || "");
      if (!text.trim()) {
        container.innerHTML = `<div class="empty">无文本内容</div>`;
        return;
      }

      const draftPreview = state.annotationDraft?.mode === "create" && state.annotationDraft.rowId === row.id && state.annotationDraft.groupId === group.id
        ? {
            id: "__selection_preview__",
            start: state.annotationDraft.start,
            end: state.annotationDraft.end,
            text: state.annotationDraft.text,
            level: "preview",
            reason: ""
          }
        : null;
      const annotations = annotationsForRow(row, group.id)
        .concat(draftPreview ? [draftPreview] : [])
        .filter((item) => item.start >= 0 && item.end <= text.length)
        .sort((a, b) => a.start - b.start);
      let cursor = 0;
      const html = annotations.map((item) => {
        const before = escapeHtml(text.slice(cursor, item.start));
        const marked = escapeHtml(text.slice(item.start, item.end));
        cursor = item.end;
        if (item.level === "preview") {
          return `${before}<span class="pe-selection-preview">${marked}</span>`;
        }
        return `${before}<span class="pe-mark ${annotationSourceClass(item)}" data-annotation-id="${escapeAttr(item.id)}" title="${escapeAttr(item.reason || "")}">${marked}</span>`;
      }).join("") + escapeHtml(text.slice(cursor));

      container.innerHTML = `<div class="annotated-prompt" data-role="pe-prompt-text">${html}</div>`;
    }

    function renderAnnotatedPePrompt(row) {
      const group = promptGroups(row)[0];
      const container = $("compareGrid").querySelector(`[data-role="prompt-content"][data-group-id="${CSS.escape(group.id)}"]`);
      renderAnnotatedPromptIn(container, row, group);
    }

    function annotationSourceClass(item) {
      return item?.source === "machine" ? "machine" : "manual";
    }

    function handlePePromptSelection(event) {
      window.setTimeout(() => {
        const selection = window.getSelection();
        if (!selection || selection.isCollapsed) return;
        const container = event.target.closest('[data-role="prompt-content"]');
        if (!container) return;
        const row = currentRow();
        const group = promptGroupById(row, container.dataset.groupId);
        if (!group?.annotatable) return;
        const root = container.querySelector('[data-role="pe-prompt-text"]');
        if (!root || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return;

        const rangeInfo = selectionOffsets(root, selection);
        if (!rangeInfo || rangeInfo.start === rangeInfo.end) return;
        const text = promptText(row, group.id);
        const start = Math.max(0, Math.min(rangeInfo.start, rangeInfo.end));
        const end = Math.min(text.length, Math.max(rangeInfo.start, rangeInfo.end));
        if (hasAnnotationOverlap(annotationsForRow(row, group.id), start, end)) {
          selection.removeAllRanges();
          toast("划线范围不能与已有划线重叠");
          return;
        }

        state.annotationDraft = { mode: "create", rowId: row.id, groupId: group.id, start, end, text: text.slice(start, end) };
        renderAnnotatedPromptIn(container, row, group);
        openAnnotationPopover(event.clientX, event.clientY, null);
      }, 0);
    }

    function handlePromptComparisonClick(event) {
      const confirmButton = event.target.closest("[data-action='confirm-machine-check']");
      if (confirmButton) {
        event.preventDefault();
        event.stopPropagation();
        confirmMachineCheck(confirmButton.dataset.groupId);
        return;
      }
      handlePePromptMarkClick(event);
    }

    function handlePePromptMarkClick(event) {
      const mark = event.target.closest(".pe-mark");
      const container = event.target.closest('[data-role="prompt-content"]');
      if (!mark || !container || !$("compareGrid").contains(mark)) return;
      const row = currentRow();
      const group = promptGroupById(row, container.dataset.groupId);
      if (!group?.annotatable) return;
      const annotation = annotationsForRow(row, group.id).find((item) => item.id === mark.dataset.annotationId);
      if (!annotation) return;
      state.annotationDraft = { mode: "edit", rowId: row.id, groupId: group.id, id: annotation.id };
      openAnnotationPopover(event.clientX, event.clientY, annotation);
    }

    async function confirmMachineCheck(groupId) {
      const row = currentRow();
      const group = promptGroupById(row, groupId);
      const issueGroup = issueGroupForPromptGroup(row, group);
      const labelMeta = parseIssueLabelRecord(issueGroup?.labels);
      const annotationMeta = annotationRecordMetaForGroup(group);
      const writes = [];

      if (issueGroup && labelMeta.unchecked) {
        writes.push(writeIssueLabelsForGroup(row, issueGroup, labelMeta.labels, { checked: true }));
      }
      if (group?.annotatable && annotationMeta.unchecked) {
        writes.push(writeAnnotationsToLark(row, group, annotationMeta.annotations, "", { silent: true, checked: true }));
      }
      if (!writes.length) {
        toast("当前栏没有待校验的机器标注");
        return;
      }

      try {
        await Promise.all(writes);
        renderPromptComparison(row);
        renderIssuePanel(row);
        applyFilters();
        toast("已确认机器标注，状态切换为已标注");
      } catch (error) {
        toast(`确认失败：${error.message}`);
      }
    }

    function selectionOffsets(root, selection) {
      const range = selection.getRangeAt(0);
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let offset = 0;
      let start = null;
      let end = null;
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node === range.startContainer) start = offset + range.startOffset;
        if (node === range.endContainer) end = offset + range.endOffset;
        offset += node.nodeValue.length;
      }
      if (start === null || end === null) return null;
      return { start, end };
    }

    function hasAnnotationOverlap(annotations, start, end, ignoreId = "") {
      return annotations.some((item) => item.id !== ignoreId && start < item.end && end > item.start);
    }

    function openAnnotationPopover(x, y, annotation) {
      const popover = $("annotationPopover");
      $("annotationTitle").textContent = annotation ? "编辑划线备注" : "新增划线备注";
      $("deleteAnnotationBtn").style.display = annotation ? "" : "none";
      $("annotationReason").value = annotation ? (annotation.reason || "") : "";
      popover.classList.add("open");
      const left = Math.min(Math.max(14, x + 10), window.innerWidth - popover.offsetWidth - 14);
      const top = Math.min(Math.max(14, y + 12), window.innerHeight - popover.offsetHeight - 14);
      popover.style.left = `${left}px`;
      popover.style.top = `${top}px`;
      $("annotationReason").focus();
    }

    function closeAnnotationPopover() {
      $("annotationPopover").classList.remove("open");
      const draft = state.annotationDraft;
      state.annotationDraft = null;
      if (draft?.mode === "create") {
        const row = currentRow();
        const group = promptGroupById(row, draft.groupId);
        const container = $("compareGrid").querySelector(`[data-role="prompt-content"][data-group-id="${CSS.escape(group.id)}"]`);
        renderAnnotatedPromptIn(container, row, group);
      }
      window.getSelection()?.removeAllRanges();
    }

    // 新增或编辑后，立即将完整 annotations JSON 覆盖写回当前 prompt 对应的标注记录列。
    async function saveAnnotationFromPopover() {
      const row = currentRow();
      const draft = state.annotationDraft;
      if (!draft) return;
      const group = promptGroupById(row, draft.groupId);
      const reason = $("annotationReason").value.trim();
      if (!reason) {
        toast("请填写错误理由");
        return;
      }

      const now = new Date().toISOString();
      let annotations = annotationsForRow(row, group.id);
      if (draft.mode === "create") {
        annotations.push({
          id: crypto.randomUUID ? crypto.randomUUID() : `ann-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          start: draft.start,
          end: draft.end,
          text: draft.text,
          level: "serious",
          source: "manual",
          reason,
          created_at: now,
          updated_at: now
        });
      } else {
        annotations = annotations.map((item) => item.id === draft.id ? { ...item, source: "manual", reason, updated_at: now } : item);
      }
      await writeAnnotationsToLark(row, group, annotations, "划线备注已同步到飞书表格");
      closeAnnotationPopover();
    }

    // 删除当前划线；无剩余 annotations 时写回空值。
    async function deleteAnnotationFromPopover() {
      const row = currentRow();
      const draft = state.annotationDraft;
      if (!draft || draft.mode !== "edit") return;
      const group = promptGroupById(row, draft.groupId);
      const annotations = annotationsForRow(row, group.id).filter((item) => item.id !== draft.id);
      await writeAnnotationsToLark(row, group, annotations, "划线已取消并同步到飞书表格");
      closeAnnotationPopover();
    }

    async function writeAnnotationsToLark(row, group, annotations, message, options = {}) {
      const normalized = annotations
        .slice()
        .sort((a, b) => a.start - b.start)
        .map((item) => ({
          id: String(item.id),
          start: item.start,
          end: item.end,
          text: String(item.text || promptText(row, group.id).slice(item.start, item.end)),
          level: item.level === "serious" || item.level === "normal" ? item.level : "serious",
          reason: String(item.reason || ""),
          created_at: item.created_at || new Date().toISOString(),
          updated_at: item.updated_at || new Date().toISOString()
        }));

      try {
        const response = await fetch("/api/annotations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            excelRow: row.excelRow,
            recordId: row.recordId || "",
            sourceUrl: activeSource?.url || "",
            sourceSheetId: activeSource?.sheetId || "",
            annotationColumn: group.annotationColumn,
            annotationsJson: normalized.length || options.checked ? serializeAnnotationRecord(normalized, { checked: options.checked }) : ""
          })
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "写入飞书表格失败");
        group.annotations = payload.value || "";
        if (promptGroups(row)[0]?.id === group.id) row.r = group.annotations;
        if (!options.silent) {
          clearCache(); // 写入成功后清除缓存
          renderPromptComparison(row);
          applyFilters(); // 重新筛选以更新列表
          toast(message);
        }
        return group.annotations;
      } catch (error) {
        if (options.silent) throw error;
        toast(`写入失败：${error.message}`);
        return "";
      }
    }

    function renderContent(containerId, value) {
      const container = $(containerId);
      renderContentElement(container, value);
    }

    function renderContentElement(container, value) {
      if (!container) return;
      const text = stripUrls(value || "").trim();
      const urls = extractUrls(value || "");
      const textHtml = text ? `<div class="text">${escapeHtml(text)}</div>` : `<div class="empty">无文本内容</div>`;
      const imageHtml = urls.length ? `
        <div class="media-grid">
          ${urls.map((url) => `<img src="${escapeAttr(url)}" alt="关联图片，点击放大查看" loading="lazy" decoding="async" title="点击放大查看" onerror="this.alt='图片加载失败'; this.style.minHeight='120px';">`).join("")}
        </div>
      ` : "";
      container.innerHTML = `${textHtml}${imageHtml}`;
    }

    function openImageViewer(src, alt) {
      $("viewerImage").src = src;
      $("viewerImage").alt = alt || "放大图片";
      $("imageViewer").classList.add("open");
    }

    function closeImageViewer() {
      $("imageViewer").classList.remove("open");
      $("viewerImage").src = "";
    }

    function moveDetail(step) {
      showDetail(state.currentIndex + step);
    }

    function problemTags(value) {
      return parseIssueLabelRecord(value).labels;
    }

    function normalizeIssueLabel(label) {
      const text = String(label || "").trim();
      if (text === "没问题-没问题") return "没问题";
      return text;
    }

    function normalizeIssueLabels(labels) {
      const normalized = [];
      labels.forEach((label) => {
        const value = normalizeIssueLabel(label);
        if (value && !normalized.includes(value)) normalized.push(value);
      });
      return normalized;
    }

    // 解析问题标签字段：兼容老文本、机器写入的 check:0 前缀，以及 JSON 结构。
    function parseIssueLabelRecord(value) {
      const raw = String(value || "").trim();
      if (!raw) return { labels: [], unchecked: false, checked: false, raw: "" };
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          const unchecked = isUncheckedCheckValue(parsed.check);
          const checked = isCheckedCheckValue(parsed.check) || parsed.checked === 1 || parsed.checked === true;
          const source = Array.isArray(parsed.labels)
            ? parsed.labels
            : Array.isArray(parsed.pe_label)
              ? parsed.pe_label
              : Array.isArray(parsed.tags)
                ? parsed.tags
                : String(parsed.labels || parsed.pe_label || parsed.tags || "");
          return { labels: normalizeIssueLabels(splitIssueLabels(source).filter((tag) => !isUncheckedCheckValue(tag) && !isCheckedCheckValue(tag))), unchecked, checked, raw };
        }
      } catch {
        // 非 JSON 的老数据继续按分隔符解析。
      }
      const parts = splitIssueLabels(raw);
      const unchecked = isUncheckedCheckValue(parts[0]);
      const checked = parts.some((tag) => isCheckedCheckValue(tag));
      return {
        labels: normalizeIssueLabels(parts.filter((tag) => !isUncheckedCheckValue(tag) && !isCheckedCheckValue(tag))),
        unchecked,
        checked,
        raw
      };
    }

    function splitIssueLabels(value) {
      const text = Array.isArray(value) ? value.join("；") : String(value || "");
      return text
        .split(/[；;，,\n]/)
        .map((tag) => tag.trim())
        .filter(Boolean);
    }

    function isUncheckedCheckValue(value) {
      return value === 0 || value === "0" || String(value || "").trim().replace("：", ":").toLowerCase() === "check:0";
    }

    function isCheckedCheckValue(value) {
      return value === 1 || value === "1" || String(value || "").trim().replace("：", ":").toLowerCase() === "check:1";
    }

    function issueGroups(row) {
      if (Array.isArray(row.issueGroups) && row.issueGroups.length) return row.issueGroups;
      return [{
        id: "issue-0",
        labelTitle: "PE问题标签",
        imageLabelTitle: "图像效果标签",
        summaryTitle: "问题分析",
        labelColumn: "",
        imageLabelColumn: "",
        summaryColumn: "P",
        imageLabels: "",
        labels: "",
        summary: row.p || ""
      }];
    }

    function allIssueTagOptions() {
      const existing = rows.flatMap((row) => issueGroups(row).flatMap((group) => problemTags(group.labels)));
      return Array.from(new Set([...labelTaxonomy.peProblemLabels, ...existing])).filter(Boolean);
    }

    function renderIssuePanel(row) {
      $("issueGrid").innerHTML = visibleIssueGroups(row).map((group) => renderIssueCard(group, row)).join("");
      $("issueGrid").querySelectorAll(".issue-card").forEach((card) => renderIssueOptions(card));
    }

    function renderIssueCard(group, row) {
      const imageLabels = problemTags(group.imageLabels);
      const peLabels = problemTags(group.labels);
      const taxonomyMode = taxonomyModeForRow(row);
      return `
        <article class="issue-card" data-issue-id="${escapeAttr(group.id)}" data-taxonomy-mode="${escapeAttr(taxonomyMode)}">
          <div class="issue-card-head">
            <span>${escapeHtml(issueAnalysisTitle(group))}</span>
            <span class="issue-actions">
              <button type="button" data-issue-action="save" title="保存本栏" aria-label="保存本栏">💾</button>
              <button type="button" data-issue-action="clear" title="清空本栏" aria-label="清空本栏">🧹</button>
            </span>
          </div>
          ${renderIssueLabelBox("image", "图像效果标签", imageLabels, Boolean(group.imageLabelColumn))}
          ${renderIssueLabelBox("pe", "PE问题标签", peLabels, Boolean(group.labelColumn))}
          <textarea data-role="summary-input" placeholder="填写问题分析，Enter 保存，Shift+Enter 换行">${escapeHtml(group.summary || "")}</textarea>
        </article>
      `;
    }

    function issueAnalysisTitle(group) {
      const title = group.summaryTitle || group.labelTitle || "问题分析";
      return title.replace(/-问题总结$/i, "-问题分析");
    }

    function renderIssueLabelBox(kind, title, labels, writable) {
      return `
        <div class="issue-label-box ${writable ? "" : "disabled"}" data-label-kind="${escapeAttr(kind)}">
          <div class="issue-label-title">${escapeHtml(title)}${writable ? "" : "（无对应列）"}</div>
            <div class="issue-tag-bar">
              <span data-role="selected-labels">${labels.map((label) => issueSelectedChip(label, writable)).join("")}</span>
              <input class="issue-tag-input" data-role="tag-input" data-label-kind="${escapeAttr(kind)}" type="text" placeholder="${writable ? "搜索选择标签" : "当前表格无该标签列"}" ${writable ? "" : "disabled"}>
            </div>
            <div class="issue-options" data-role="tag-options"></div>
          </div>
      `;
    }

    function issueSelectedChip(label, removable = true) {
      return `<span class="tag-chip" data-label="${escapeAttr(label)}">${escapeHtml(label)}${removable ? `<button type="button" data-issue-action="remove-label" title="移除标签" aria-label="移除 ${escapeAttr(label)}">×</button>` : ""}</span>`;
    }

    function issueCardLabels(card, kind = "pe") {
      return Array.from(card.querySelectorAll(`.issue-label-box[data-label-kind="${kind}"] [data-role='selected-labels'] .tag-chip`)).map((chip) => chip.dataset.label).filter(Boolean);
    }

    function renderSelectedLabels(card, kind, labels) {
      const labelBox = card.querySelector(`.issue-label-box[data-label-kind="${kind}"]`);
      if (!labelBox) return;
      const box = labelBox.querySelector("[data-role='selected-labels']");
      box.innerHTML = labels.map((label) => issueSelectedChip(label, !labelBox.classList.contains("disabled"))).join("");
      renderIssueOptions(card, kind);
    }

    function taxonomyModeForRow(row) {
      const title = `${activeSource?.title || ""} ${activeSource?.url || ""}`.toLowerCase();
      if (title.includes("i2i")) return "i2i";
      return extractUrls(row?.c || "").length ? "i2i" : "t2i";
    }

    function taxonomyOptions(kind, context = null) {
      const mode = typeof context === "string" ? context : context?.dataset?.taxonomyMode || "t2i";
      const taxonomy = labelTaxonomy.taxonomies?.[mode] || labelTaxonomy.taxonomies?.t2i || labelTaxonomy;
      return kind === "image" ? (taxonomy.imageEffectLabels || []) : (taxonomy.peProblemLabels || []);
    }

    function splitTaxonomyOption(label) {
      const text = String(label || "").trim();
      const dashIndex = text.indexOf("-");
      if (dashIndex < 0) return { category: text, leaf: text, value: text, direct: true };
      return {
        category: text.slice(0, dashIndex).trim(),
        leaf: text.slice(dashIndex + 1).trim(),
        value: text,
        direct: false
      };
    }

    function taxonomyTree(kind, query = "", selected = new Set(), context = null) {
      const normalizedQuery = String(query || "").trim().toLowerCase();
      const options = taxonomyOptions(kind, context)
        .map(splitTaxonomyOption)
        .filter((item) => item.value && (!normalizedQuery || item.value.toLowerCase().includes(normalizedQuery) || item.category.toLowerCase().includes(normalizedQuery) || item.leaf.toLowerCase().includes(normalizedQuery)));
      const byCategory = new Map();
      options.forEach((item) => {
        if (!byCategory.has(item.category)) byCategory.set(item.category, []);
        byCategory.get(item.category).push(item);
      });
      selected.forEach((label) => {
        const item = splitTaxonomyOption(label);
        if (!byCategory.has(item.category)) byCategory.set(item.category, []);
        if (!byCategory.get(item.category).some((option) => option.value === item.value)) byCategory.get(item.category).push(item);
      });
      return Array.from(byCategory, ([category, items]) => ({ category, items }));
    }

    function activeIssueCategory(labelBox, tree) {
      const current = labelBox.dataset.activeCategory || "";
      if (tree.some((group) => group.category === current)) return current;
      return tree[0]?.category || "";
    }

    function renderIssueOptions(card, kind = "") {
      const boxes = kind ? [card.querySelector(`.issue-label-box[data-label-kind="${kind}"]`)].filter(Boolean) : Array.from(card.querySelectorAll(".issue-label-box"));
      boxes.forEach((labelBox) => {
        if (labelBox.classList.contains("disabled")) {
          labelBox.querySelector("[data-role='tag-options']").innerHTML = "";
          return;
        }
        const labelKind = labelBox.dataset.labelKind || "pe";
        const query = labelBox.querySelector("[data-role='tag-input']")?.value.trim().toLowerCase() || "";
        const selected = new Set(issueCardLabels(card, labelKind));
        const tree = taxonomyTree(labelKind, query, selected, card);
        const activeCategory = activeIssueCategory(labelBox, tree);
        labelBox.dataset.activeCategory = activeCategory;
        const activeItems = tree.find((group) => group.category === activeCategory)?.items || [];
        const activeIsSingleDirect = activeItems.length === 1 && activeItems[0].direct;
        labelBox.querySelector("[data-role='tag-options']").innerHTML = tree.length ? `
          <div class="issue-option-pane">
            ${tree.map((group) => {
              const selectedCount = group.items.filter((item) => selected.has(item.value)).length;
              const singleDirect = group.items.length === 1 && group.items[0].direct;
              const action = singleDirect ? "toggle-label" : "select-category";
              const label = singleDirect ? group.items[0].value : "";
              return `
                <button type="button" class="issue-option ${group.category === activeCategory ? "current" : ""} ${selectedCount ? "active" : ""}" data-issue-action="${action}" data-category="${escapeAttr(group.category)}" data-label="${escapeAttr(label)}">
                  <span class="option-main">
                    <span class="option-check"></span>
                    <span class="option-text">${escapeHtml(group.category)}${selectedCount ? ` (${selectedCount})` : ""}</span>
                  </span>
                  ${singleDirect ? "" : `<span class="option-arrow">›</span>`}
                </button>
              `;
            }).join("")}
          </div>
          ${activeIsSingleDirect ? "" : `<div class="issue-option-pane">
            ${activeItems.map((item) => `
              <button type="button" class="issue-option ${selected.has(item.value) ? "active" : ""}" data-issue-action="toggle-label" data-label="${escapeAttr(item.value)}">
                <span class="option-main">
                  <span class="option-check"></span>
                  <span class="option-text">${escapeHtml(item.leaf)}</span>
                </span>
              </button>
            `).join("") || `<span class="empty">无可选标签</span>`}
          </div>`}
        ` : `<span class="empty">无匹配标签</span>`;
      });
    }

    function handleIssuePanelClick(event) {
      if (state.suppressIssueClick) {
        state.suppressIssueClick = false;
        return;
      }
      const card = event.target.closest(".issue-card");
      const action = event.target.closest("[data-issue-action]")?.dataset.issueAction;
      if (!card || !action) return;
      const label = event.target.closest("[data-label]")?.dataset.label || "";
      const category = event.target.closest("[data-category]")?.dataset.category || "";
      performIssueAction(card, action, label, event, category);
    }

    function handleIssuePanelMouseDown(event) {
      const actionTarget = event.target.closest(".issue-options [data-issue-action]");
      if (!actionTarget) return;
      event.preventDefault();
      state.suppressIssueClick = true;
      const card = event.target.closest(".issue-card");
      const action = actionTarget.dataset.issueAction;
      const label = actionTarget.closest("[data-label]")?.dataset.label || "";
      const category = actionTarget.closest("[data-category]")?.dataset.category || "";
      performIssueAction(card, action, label, event, category);
      window.setTimeout(() => {
        state.suppressIssueClick = false;
      }, 250);
    }

    function performIssueAction(card, action, label, event, category = "") {
      const kind = event?.target?.closest(".issue-label-box")?.dataset.labelKind || "pe";
      if (action === "select-category") {
        const labelBox = event?.target?.closest(".issue-label-box");
        if (labelBox) {
          labelBox.dataset.activeCategory = category;
          renderIssueOptions(card, kind);
        }
      }
      if (action === "toggle-label") toggleIssueLabel(card, kind, label);
      if (action === "remove-label") toggleIssueLabel(card, kind, label);
      if (action === "save") saveIssueCard(card);
      if (action === "clear") clearIssueCard(card);
    }

    function handleIssueOptionPointerDown(event) {
      const actionTarget = event.target.closest?.(".issue-options [data-issue-action]");
      if (!actionTarget) return;
      event.preventDefault();
      event.stopPropagation();
      state.suppressIssueClick = true;
      const card = actionTarget.closest(".issue-card");
      const action = actionTarget.dataset.issueAction;
      const label = actionTarget.closest("[data-label]")?.dataset.label || "";
      const category = actionTarget.closest("[data-category]")?.dataset.category || "";
      performIssueAction(card, action, label, event, category);
      window.setTimeout(() => {
        state.suppressIssueClick = false;
      }, 250);
    }

    function handleIssuePanelInput(event) {
      const card = event.target.closest(".issue-card");
      if (!card || !event.target.matches("[data-role='tag-input']")) return;
      renderIssueOptions(card, event.target.dataset.labelKind || "pe");
    }

    function handleIssuePanelFocusIn(event) {
      const card = event.target.closest(".issue-card");
      if (!card) return;
      if (event.target.matches("[data-role='tag-input']")) {
        card.querySelectorAll(".issue-label-box").forEach((box) => box.classList.remove("open"));
        const labelBox = event.target.closest(".issue-label-box");
        labelBox?.classList.add("open");
        renderIssueOptions(card, event.target.dataset.labelKind || "pe");
      } else {
        card.querySelectorAll(".issue-label-box").forEach((box) => box.classList.remove("open"));
      }
    }

    function handleIssuePanelFocusOut(event) {
      const card = event.target.closest(".issue-card");
      if (!card) return;
      window.setTimeout(() => {
        if (!card.contains(document.activeElement)) card.querySelectorAll(".issue-label-box").forEach((box) => box.classList.remove("open"));
      }, 180);
    }

    async function handleIssuePanelKeydown(event) {
      const card = event.target.closest(".issue-card");
      if (!card) return;
      if (event.target.matches("[data-role='tag-input']") && event.key === "Enter" && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        const kind = event.target.dataset.labelKind || "pe";
        const value = event.target.value.trim().toLowerCase();
        const match = taxonomyOptions(kind, card).find((label) => label.toLowerCase() === value);
        if (match) {
          await toggleIssueLabel(card, kind, match, true);
          event.target.value = "";
          renderIssueOptions(card, kind);
        } else {
          toast("只能选择标签体系中的标签");
        }
        return;
      }
      if (event.target.matches("[data-role='summary-input']") && event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        saveIssueCard(card);
      }
    }

    async function toggleIssueLabel(card, kind, label, forceOn = false) {
      const labels = issueCardLabels(card, kind);
      const exists = labels.includes(label);
      const next = forceOn || !exists ? Array.from(new Set([...labels, label])) : labels.filter((item) => item !== label);
      renderSelectedLabels(card, kind, next);
      await saveIssueLabels(card, kind, next);
    }

    async function saveIssueLabels(card, kind, labels) {
      const row = currentRow();
      const group = issueGroups(row).find((item) => item.id === card.dataset.issueId);
      if (!group) return;
      try {
        if (kind === "image") {
          await writeIssueLabelsForGroup(row, group, issueCardLabels(card, "pe"), { imageLabels: labels, writeLabels: false, writeImageLabels: true });
          group.imageLabels = labels.join("；");
          toast("图像效果标签已保存");
        } else {
          await writeIssueLabelsForGroup(row, group, labels, { imageLabels: issueCardLabels(card, "image"), writeLabels: true, writeImageLabels: false });
          group.labels = labels.join("；");
          toast("PE问题标签已保存");
        }
        renderIssueOptions(card, kind);
      } catch (error) {
        toast(`写入失败：${error.message}`);
      }
    }

    async function writeIssueLabelsForGroup(row, group, labels, options = {}) {
      const labelsToWrite = options.checked && !labels.length ? ["check:1"] : labels;
      const imageLabelsToWrite = options.imageLabels || [];
      if (options.writeLabels !== false) group.labels = labelsToWrite.join("；");
      if (options.writeImageLabels === true) group.imageLabels = imageLabelsToWrite.join("；");
      const response = await fetch("/api/issues", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          excelRow: row.excelRow,
          recordId: row.recordId || "",
          sourceUrl: activeSource?.url || "",
          sourceSheetId: activeSource?.sheetId || "",
          imageLabelColumn: group.imageLabelColumn,
          labelColumn: group.labelColumn,
          summaryColumn: group.summaryColumn,
          imageLabels: imageLabelsToWrite,
          labels: labelsToWrite,
          summary: "",
          writeImageLabels: options.writeImageLabels === true,
          writeLabels: options.writeLabels !== false,
          writeSummary: false
        })
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "写入飞书表格失败");
      group.imageLabels = payload.value?.imageLabels || group.imageLabels || "";
      group.labels = payload.value?.labels || group.labels;
      return group.labels;
    }

    async function saveIssueCard(card) {
      const row = currentRow();
      const group = issueGroups(row).find((item) => item.id === card.dataset.issueId);
      if (!group) return;
      const imageLabels = issueCardLabels(card, "image");
      const labels = issueCardLabels(card, "pe");
      const summary = card.querySelector("[data-role='summary-input']").value.trim();
      await writeIssueToLark(row, group, imageLabels, labels, summary, "本栏问题分析已保存");
    }

    async function clearIssueCard(card) {
      const row = currentRow();
      const group = issueGroups(row).find((item) => item.id === card.dataset.issueId);
      if (!group) return;
      card.querySelector("[data-role='summary-input']").value = "";
      renderSelectedLabels(card, "image", []);
      renderSelectedLabels(card, "pe", []);
      await writeIssueToLark(row, group, [], [], "", "本栏问题分析已清空");
    }

    async function writeIssueToLark(row, group, imageLabels, labels, summary, message) {
      const buttons = Array.from($("issuePanel").querySelectorAll("button"));
      buttons.forEach((button) => button.disabled = true);
      try {
        const response = await fetch("/api/issues", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            excelRow: row.excelRow,
            recordId: row.recordId || "",
            sourceUrl: activeSource?.url || "",
            sourceSheetId: activeSource?.sheetId || "",
            imageLabelColumn: group.imageLabelColumn,
            labelColumn: group.labelColumn,
            summaryColumn: group.summaryColumn,
            imageLabels,
            labels,
            summary,
            writeImageLabels: true,
            writeLabels: true,
            writeSummary: true
          })
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "写入飞书表格失败");
        group.imageLabels = payload.value?.imageLabels || "";
        group.labels = payload.value?.labels || "";
        group.summary = payload.value?.summary || "";
        row.p = issueGroups(row).map((item) => item.summary).filter(Boolean).join("；");
        clearCache(); // 写入成功后清除缓存
        renderIssuePanel(row);
        applyFilters(); // 重新筛选以更新列表
        toast(message);
      } catch (error) {
        toast(`写入失败：${error.message}`);
      } finally {
        buttons.forEach((button) => button.disabled = false);
      }
    }

    function currentRow() {
      return state.filtered[state.currentIndex];
    }

    function extractUrls(value) {
      return value.match(urlPattern) || [];
    }

    function stripUrls(value) {
      return value.replace(urlPattern, "").trim();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function escapeAttr(value) {
      return escapeHtml(value).replaceAll("`", "&#096;");
    }

    function toast(message) {
      const el = $("toast");
      el.textContent = message;
      el.classList.add("show");
      window.clearTimeout(toast.timer);
      toast.timer = window.setTimeout(() => el.classList.remove("show"), 1800);
    }

    init();
