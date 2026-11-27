/*
 * core.js：前後端共用的純函式（沒有 DOM、沒有網路請求）。
 *
 *   瀏覽器：<script src="core.js"></script> 之後用 window.PortfolioCore
 *   Node  ：const C = require("./core.js")（tests/js/run_golden.js）
 *
 * 每個函式都對應 portfolio_data.py / app.py 的同名規則，輸出由 tests/golden/*.json 約束兩邊一致
 * （python tests/make_golden.py 重新產生 golden；pytest 與 node tests/js/run_golden.js 都會比對）。
 * 門檻一律由呼叫端從 window.APP_CONFIG（config.json）傳入，這裡不寫死任何設定值。
 */
(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    } else {
        root.PortfolioCore = api;
    }
})(typeof self !== "undefined" ? self : this, function () {
    "use strict";

    // 合理的報價時間範圍（2000-01-01 ~ 2100-01-01，epoch 秒）；擋掉毫秒或亂碼時間戳
    const TS_MIN = 946684800;
    const TS_MAX = 4102444800;
    const MAX_PAYMENTS = 1200; // 還款期數上限（100 年），資料異常時避免無窮迴圈

    function hasOwn(obj, key) {
        return obj !== null && obj !== undefined && Object.prototype.hasOwnProperty.call(obj, key);
    }

    function isPlainObject(value) {
        return value !== null && typeof value === "object" && !Array.isArray(value);
    }

    function nullish(value) {
        return value === undefined ? null : value;
    }

    function requireNumbers(cfg, keys, label) {
        keys.forEach(key => {
            if (!cfg || typeof cfg[key] !== "number" || !Number.isFinite(cfg[key])) {
                throw new Error("缺少設定 " + label + "." + key + "（config.json）");
            }
        });
    }

    // ------------------------------------------------------------------ 文字

    const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};

    /** HTML 跳脫：所有外部資料（名稱、代號、銀行、錯誤訊息）放進 innerHTML 前一律經過這裡。 */
    function esc(value) {
        if (value === null || value === undefined) return "";
        return String(value).replace(/[&<>"']/g, ch => ESC_MAP[ch]);
    }

    // ------------------------------------------------------------------ 數值（對應 Python _to_float 等）

    // Python float() 接受的字串：可有正負號、底線分隔的數字、小數點、指數（inf / nan 轉出來不是有限數，一樣視為無效）
    const FLOAT_RE = /^[+-]?(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?$/;
    const INT_RE = /^[+-]?\d(?:_?\d)*$/;

    /** 對應 _to_float：None / "" / "-" / 無法解析 / 非有限數 → null。 */
    function toFloat(value) {
        if (value === null || value === undefined || value === "" || value === "-") return null;
        let num;
        if (typeof value === "number") {
            num = value;
        } else if (typeof value === "boolean") {
            num = value ? 1 : 0;
        } else if (typeof value === "string") {
            const text = value.trim();
            if (!FLOAT_RE.test(text)) return null;
            num = Number(text.replace(/_/g, ""));
        } else {
            return null;
        }
        return Number.isFinite(num) ? num : null;
    }

    /** 對應 _to_positive_float：只接受 > 0 的有限數（濾除 TWSE 市價單編碼 0.0000）。 */
    function toPositive(value) {
        const num = toFloat(value);
        return num !== null && num > 0 ? num : null;
    }

    /** 對應 _to_int：int(float(x))，向 0 取整。 */
    function toInt(value) {
        const num = toFloat(value);
        return num !== null ? Math.trunc(num) + 0 : null;
    }

    // Python int(x)（_market_date 用）：數字向 0 取整；字串必須是整數格式
    function pyInt(value) {
        if (typeof value === "number") return Number.isFinite(value) ? Math.trunc(value) + 0 : null;
        if (typeof value === "boolean") return value ? 1 : 0;
        if (typeof value === "string") {
            const text = value.trim();
            return INT_RE.test(text) ? Number(text.replace(/_/g, "")) : null;
        }
        return null;
    }

    function isValidPrice(value) {
        return typeof value === "number" && Number.isFinite(value) && value > 0;
    }

    function isValidTs(value) {
        return typeof value === "number" && value >= TS_MIN && value <= TS_MAX;
    }

    /**
     * 對應 Python round(x, digits)：依 double 的精確十進位值四捨五入，剛好一半時取偶數。
     * Number(x.toFixed(d)) 在剛好一半時會遠離 0 進位（0.125 → 0.13，Python 得 0.12），
     * 所以先用 toFixed(100) 取得精確展開判斷是否剛好一半。
     */
    function round(value, digits) {
        if (typeof value !== "number" || !Number.isFinite(value)) return value;
        const d = digits || 0;
        const abs = Math.abs(value);
        if (abs >= 1e21) return value;
        let out = Number(abs.toFixed(d));
        // 只有接近「剛好一半」時才做較慢的精確檢查（五線譜一次要捨入上萬個數字）
        const scaled = abs * Math.pow(10, d);
        if (Math.abs(scaled - Math.floor(scaled) - 0.5) <= Math.max(1e-6, scaled * 4e-16)) {
            const exact = abs.toFixed(100);
            const dot = exact.indexOf(".");
            if (/^50*$/.test(exact.slice(dot + 1 + d))) {
                const kept = d > 0 ? exact.slice(0, dot + 1 + d) : exact.slice(0, dot);
                if (Number(kept[kept.length - 1]) % 2 === 0) out = Number(kept);
            }
        }
        return value < 0 ? -out : out;
    }

    /** Neumaier 補償加總，與 CPython 3.12+ sum() 及 portfolio_data._compensated_sum 逐位元一致。 */
    function compensatedSum(values) {
        let total = 0;
        let comp = 0;
        for (let i = 0; i < values.length; i++) {
            const x = values[i];
            const t = total + x;
            if (Math.abs(total) >= Math.abs(x)) comp += (total - t) + x;
            else comp += (x - t) + total;
            total = t;
        }
        if (comp && Number.isFinite(comp)) total += comp;
        return total;
    }

    // ------------------------------------------------------------------ 代號

    function normTicker(ticker) {
        return String(ticker || "").trim().toUpperCase();
    }

    function isTaiwanTicker(ticker) {
        const upper = normTicker(ticker);
        return upper.endsWith(".TW") || upper.endsWith(".TWO");
    }

    /** cashTickers 來自 config.json（APP_CONFIG.cashTickers）。 */
    function isCash(ticker, cashTickers) {
        if (!Array.isArray(cashTickers)) throw new Error("缺少設定 cashTickers（config.json）");
        const upper = normTicker(ticker);
        return cashTickers.some(t => normTicker(t) === upper);
    }

    /** 2330.TW → tse_2330.tw（上市）；6488.TWO → otc_6488.tw（上櫃）。 */
    function twseChannel(ticker) {
        const code = ticker.split(".")[0];
        return (ticker.toUpperCase().endsWith(".TWO") ? "otc_" : "tse_") + code + ".tw";
    }

    /** 2330.TW → TWS:2330:STOCK；QQQM → USS:QQQM:STOCK。 */
    function cnyesSymbol(ticker) {
        const upper = ticker.trim().toUpperCase();
        if (isTaiwanTicker(upper)) return "TWS:" + upper.split(".")[0] + ":STOCK";
        return "USS:" + upper + ":STOCK";
    }

    /** {頻道: 代號}（只含台股）。TWSE 每次最多查 batchSize 個頻道，以 | 串接。 */
    function twseChannelMap(tickers) {
        const out = {};
        (tickers || []).forEach(t => {
            const upper = t.trim().toUpperCase();
            if (isTaiwanTicker(upper)) out[twseChannel(upper)] = upper;
        });
        return out;
    }

    /** {鉅亨網符號: 代號}（排除現金）。 */
    function cnyesSymbolMap(tickers, cashTickers) {
        const out = {};
        (tickers || []).forEach(t => {
            const upper = t.trim().toUpperCase();
            if (upper && !isCash(upper, cashTickers)) out[cnyesSymbol(upper)] = upper;
        });
        return out;
    }

    // ------------------------------------------------------------------ 日期

    function pad(num, width) {
        return String(num).padStart(width, "0");
    }

    /** 對應 _market_date：epoch 秒 + gmtoffset → 交易所當地日期 YYYY-MM-DD。 */
    function marketDate(ts, gmtoffset) {
        const t = pyInt(ts);
        const off = gmtoffset ? pyInt(gmtoffset) : 0;
        if (t === null || off === null) return null;
        const date = new Date((t + off) * 1000);
        if (Number.isNaN(date.getTime())) return null;
        return pad(date.getUTCFullYear(), 4) + "-" + pad(date.getUTCMonth() + 1, 2) + "-" + pad(date.getUTCDate(), 2);
    }

    const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

    /** "YYYY-MM-DD" → 本機時區當天 00:00 的 Date；格式或日期不合法回傳 null。 */
    function parseLocalDate(text) {
        const match = typeof text === "string" ? DATE_RE.exec(text) : null;
        if (!match) return null;
        const y = Number(match[1]);
        const m = Number(match[2]) - 1;
        const d = Number(match[3]);
        const date = new Date(y, m, d);
        date.setFullYear(y); // 0000–0099 年不要被當成 1900 年代
        if (date.getFullYear() !== y || date.getMonth() !== m || date.getDate() !== d) return null;
        return date;
    }

    /** Date → 本機時區的 YYYY-MM-DD（不用 toISOString：那是 UTC 日期，台北早上 8 點前會差一天）。 */
    function localDateStr(date) {
        return pad(date.getFullYear(), 4) + "-" + pad(date.getMonth() + 1, 2) + "-" + pad(date.getDate(), 2);
    }

    function zonedDateStr(date, timeZone) {
        const parts = {};
        new Intl.DateTimeFormat("en-CA", {timeZone, year: "numeric", month: "2-digit", day: "2-digit"})
            .formatToParts(date)
            .forEach(part => { parts[part.type] = part.value; });
        return parts.year + "-" + parts.month + "-" + parts.day;
    }

    /** 台北日期（與 Python today_tpe 一致，不受瀏覽器所在時區影響）。 */
    function taipeiDateStr(date) {
        return zonedDateStr(date || new Date(), "Asia/Taipei");
    }

    /** 報價時間（epoch 秒）→ 交易所當地日期：台股用台北、美股用紐約（盤後時段不會被算成隔天）。 */
    function exchangeDateStr(epochSec, isTaiwan) {
        if (!isValidTs(epochSec)) return null;
        return zonedDateStr(new Date(epochSec * 1000), isTaiwan ? "Asia/Taipei" : "America/New_York");
    }

    /** 對應 days_old：YYYY-MM-DD 距 todayStr（預設台北今天）幾天；格式不對回傳 null。 */
    function daysOld(dateStr, todayStr) {
        const match = typeof dateStr === "string" ? DATE_RE.exec(dateStr) : null;
        const today = DATE_RE.exec(todayStr || taipeiDateStr());
        if (!match || !today) return null;
        const a = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
        const check = new Date(a);
        if (check.getUTCFullYear() !== Number(match[1]) || check.getUTCMonth() !== Number(match[2]) - 1
            || check.getUTCDate() !== Number(match[3])) return null;
        const b = Date.UTC(Number(today[1]), Number(today[2]) - 1, Number(today[3]));
        return Math.round((b - a) / 86400000);
    }

    /** ISO 時間字串距 now 幾分鐘（無法解析回傳 null）。 */
    function minutesSince(isoText, now) {
        const t = Date.parse(isoText);
        if (Number.isNaN(t)) return null;
        return Math.floor(((now || new Date()).getTime() - t) / 60000);
    }

    /** 靜態快照新鮮度：fresh / warn / alert（門檻見 config.json freshness）。 */
    function freshnessLevel(minutes, cfg) {
        requireNumbers(cfg, ["warnMinutes", "alertMinutes"], "freshness");
        if (minutes === null || minutes === undefined || minutes >= cfg.alertMinutes) return "alert";
        if (minutes >= cfg.warnMinutes) return "warn";
        return "fresh";
    }

    /**
     * 把最新一點併入時間序列（不改動原陣列）：比最後一天新 → 附加；同一天 → 取代；較舊或缺值 → 不變。
     * 用於即時刷新後的歷史曲線最後一點與五線譜最新收盤。
     */
    function upsertLastPoint(dates, values, date, value) {
        const outDates = dates.slice();
        const outValues = values.slice();
        if (!date || typeof value !== "number" || !Number.isFinite(value)) return {dates: outDates, values: outValues};
        const last = outDates.length ? outDates[outDates.length - 1] : null;
        if (last === null || date > last) {
            outDates.push(date);
            outValues.push(value);
        } else if (date === last) {
            outValues[outValues.length - 1] = value;
        }
        return {dates: outDates, values: outValues};
    }

    // ------------------------------------------------------------------ Yahoo chart

    function chartCloses(result) {
        const indicators = (result && result.indicators) || {};
        const quote = (indicators.quote || [{}])[0] || {};
        return quote.close || [];
    }

    /** 對應 _daily_bars：[[交易所當地日期, 收盤價, 時間戳]]，只保留 > 0 的收盤價。 */
    function dailyBars(result) {
        const meta = (result && result.meta) || {};
        const offset = meta.gmtoffset || 0;
        const stamps = (result && result.timestamp) || [];
        const closes = chartCloses(result);
        const n = Math.min(stamps.length || 0, closes.length || 0);
        const bars = [];
        for (let i = 0; i < n; i++) {
            const val = toPositive(closes[i]);
            const date = val !== null ? marketDate(stamps[i], offset) : null;
            if (date) bars.push([date, val, pyInt(stamps[i])]);
        }
        return bars;
    }

    /**
     * 對應 _prev_close_from_chart：meta.previousClose → 日線倒數第二根（最後一根是今天時）或最後一根
     * → meta.chartPreviousClose（最後手段；range=5d 時那是約 5 個交易日前的收盤）。
     */
    function prevCloseFromChart(result) {
        const meta = (result && result.meta) || {};
        const prev = toPositive(meta.previousClose);
        if (prev !== null) return prev;
        const bars = dailyBars(result);
        if (bars.length) {
            const today = marketDate(meta.regularMarketTime, meta.gmtoffset);
            if (bars[bars.length - 1][0] !== today) return bars[bars.length - 1][1];
            if (bars.length >= 2) return bars[bars.length - 2][1];
        }
        return toPositive(meta.chartPreviousClose);
    }

    /** 對應 yahoo_quote_from_chart → {price, prevClose, asOf, name}。 */
    function yahooQuoteFromChart(result) {
        if (!result) return {price: null, prevClose: null, asOf: null, name: null};
        const meta = result.meta || {};
        let price = toPositive(meta.regularMarketPrice);
        let asOf = toInt(meta.regularMarketTime);
        if (price === null) {
            const bars = dailyBars(result);
            if (bars.length) {
                price = bars[bars.length - 1][1];
                asOf = bars[bars.length - 1][2];
            }
        }
        return {
            price,
            prevClose: prevCloseFromChart(result),
            asOf,
            name: meta.shortName || meta.longName || null,
        };
    }

    /** 對應 vix_from_chart → {value, previousValue, date}。 */
    function vixFromChart(result) {
        const meta = (result && result.meta) || {};
        const bars = dailyBars(result);
        let price = toPositive(meta.regularMarketPrice);
        if (price === null && bars.length) price = bars[bars.length - 1][1];
        const date = meta.regularMarketTime
            ? marketDate(meta.regularMarketTime, meta.gmtoffset)
            : (bars.length ? bars[bars.length - 1][0] : null);
        const prev = prevCloseFromChart(result);
        return {
            value: price !== null ? round(price, 2) : null,
            previousValue: prev !== null ? round(prev, 2) : null,
            date,
        };
    }

    /** 對應 _fx_plausible：匯率合理範圍（config.json fx.min / fx.max）。 */
    function fxPlausible(rate, cfg) {
        requireNumbers(cfg, ["min", "max"], "fx");
        return isValidPrice(rate) && rate >= cfg.min && rate <= cfg.max;
    }

    /** 對應 _yahoo_usd_twd_info：TWD=X chart → {value, source, asOf, isFallback}；不合理回傳 null。 */
    function fxInfoFromYahoo(result, cfg) {
        const detail = yahooQuoteFromChart(result);
        if (!fxPlausible(detail.price, cfg)) return null;
        return {value: detail.price, source: "yahoo", asOf: detail.asOf, isFallback: false};
    }

    // ------------------------------------------------------------------ TWSE / 鉅亨網

    function firstPositiveFromBook(raw) {
        if (!raw || raw === "-") return null;
        const parts = String(raw).split("_");
        for (let i = 0; i < parts.length; i++) {
            const val = toPositive(parts[i]);
            if (val !== null) return val;
        }
        return null;
    }

    /** 對應 _parse_twse_row：成交價 z → 最佳買價 b → 最佳賣價 a；都沒有就是 null，不拿昨收 y 冒充。 */
    function parseTwseRow(row) {
        const price = toPositive(row.z) || firstPositiveFromBook(row.b) || firstPositiveFromBook(row.a);
        const tlong = toFloat(row.tlong);
        return {
            price: price || null,
            prevClose: toPositive(row.y),
            asOf: tlong ? Math.trunc(tlong / 1000) + 0 : null,
            name: row.n || null,
        };
    }

    /** 對應 _parse_cnyes_row：6 = 最新價、21 = 昨收、200007 = 報價時間（epoch 秒）、200009 = 名稱。 */
    function parseCnyesRow(row) {
        return {
            price: toPositive(row["6"]),
            prevClose: toPositive(row["21"]),
            asOf: toInt(row["200007"]),
            name: row["200009"] || null,
        };
    }

    /** 對應 match_twse_rows：以 ex + "_" + ch 對回頻道。 */
    function matchTwseRows(rows, channelMap) {
        const out = {};
        (rows || []).forEach(row => {
            if (!isPlainObject(row)) return;
            const key = String(nullish(row.ex) === null ? "None" : row.ex) + "_"
                + String(nullish(row.ch) === null ? "None" : row.ch);
            if (hasOwn(channelMap, key) && channelMap[key]) out[channelMap[key]] = parseTwseRow(row);
        });
        return out;
    }

    /** 對應 match_cnyes_rows：以欄位 "0"（或 "800013"）的符號對回代號。 */
    function matchCnyesRows(rows, symbolMap) {
        const out = {};
        (rows || []).forEach(row => {
            if (!isPlainObject(row)) return;
            const symbol = row["0"] || row["800013"];
            if (typeof symbol === "string" && hasOwn(symbolMap, symbol) && symbolMap[symbol]) {
                out[symbolMap[symbol]] = parseCnyesRow(row);
            }
        });
        return out;
    }

    // ------------------------------------------------------------------ 共識報價

    function relDiff(p1, p2) {
        return Math.abs(p1 - p2) / Math.max(p1, p2);
    }

    /**
     * 對應 resolve_consensus（規則說明見 portfolio_data.py）。cfg = APP_CONFIG.consensus。
     * 回傳 {price, source, conflict, stale, sources: {來源: 價格}, asOf}
     */
    function resolveConsensus(quotes, priority, cfg) {
        requireNumbers(cfg, ["twoSourceMaxDiff", "staleGapSeconds"], "consensus");
        const maxDiff = cfg.twoSourceMaxDiff;
        const staleGap = cfg.staleGapSeconds;
        const q = {};
        Object.keys(quotes || {}).forEach(src => {
            if (isPlainObject(quotes[src])) q[src] = quotes[src];
        });
        const order = priority.filter(s => hasOwn(q, s))
            .concat(Object.keys(q).filter(s => priority.indexOf(s) < 0).sort());
        let valid = order.filter(s => isValidPrice(q[s].price));
        const result = {price: null, source: null, conflict: false, stale: false, sources: {}, asOf: null};
        valid.forEach(s => { result.sources[s] = q[s].price; });

        const asOf = src => (isValidTs(q[src].asOf) ? q[src].asOf : null);
        const dated = valid.map(asOf).filter(ts => ts !== null);
        if (dated.length >= 2) {
            const newest = Math.max.apply(null, dated);
            valid = valid.filter(s => asOf(s) === null || newest - asOf(s) <= staleGap);
        }
        const pick = (src, conflict) => Object.assign(result, {
            price: q[src].price, source: src, conflict: !!conflict, asOf: asOf(src),
        });

        if (!valid.length) {
            for (let i = 0; i < order.length; i++) {
                const prev = q[order[i]].prevClose;
                if (isValidPrice(prev)) {
                    Object.assign(result, {price: prev, source: order[i] + ":prevClose", stale: true});
                    break;
                }
            }
            return result;
        }
        if (valid.length === 1) return pick(valid[0], false);
        if (valid.length >= 3) {
            const ranked = valid.slice().sort((a, b) => q[a].price - q[b].price);
            const prices = valid.map(s => q[s].price);
            let agree = false;
            for (let i = 0; i < prices.length && !agree; i++) {
                for (let j = i + 1; j < prices.length; j++) {
                    if (relDiff(prices[i], prices[j]) <= maxDiff) { agree = true; break; }
                }
            }
            return pick(ranked[Math.floor(ranked.length / 2)], !agree);
        }
        const first = valid[0];
        const second = valid[1];
        if (relDiff(q[first].price, q[second].price) <= maxDiff) return pick(first, false);
        const newest = (asOf(second) || 0) > (asOf(first) || 0) ? second : first;
        return pick(newest, true);
    }

    const TW_PRIORITY = ["twse", "cnyes", "yahoo"];
    const US_PRIORITY = ["yahoo", "cnyes"];

    function quoteMeta(consensus) {
        return {
            source: consensus.source,
            conflict: consensus.conflict,
            stale: consensus.stale,
            sources: consensus.sources,
            asOf: consensus.asOf,
        };
    }

    /** 對應 _tw_quote：台股三源共識（台幣）。 */
    function twQuote(twse, cnyes, yahoo, cfg) {
        twse = twse || {};
        cnyes = cnyes || {};
        yahoo = yahoo || {};
        const consensus = resolveConsensus({twse, cnyes, yahoo}, TW_PRIORITY, cfg);
        return Object.assign({
            name: twse.name || cnyes.name || nullish(yahoo.name),
            price: consensus.price,
            currency: "TWD",
        }, quoteMeta(consensus));
    }

    /** 對應 _us_quote：美股兩源共識（美元），以匯率換成台幣；sources 內為美元報價。 */
    function usQuote(yahoo, cnyes, fxRate, cfg) {
        yahoo = yahoo || {};
        cnyes = cnyes || {};
        const consensus = resolveConsensus({yahoo, cnyes}, US_PRIORITY, cfg);
        const usd = consensus.price;
        return Object.assign({
            name: yahoo.name || nullish(cnyes.name),
            price: usd !== null && fxRate ? round(usd * fxRate, 2) : null,
            priceUsd: usd,
            currency: "TWD",
            fxRate: nullish(fxRate),
        }, quoteMeta(consensus));
    }

    // ------------------------------------------------------------------ 樂活五線譜

    /**
     * 對應 compute_lohas：最近 window 個交易日收盤價線性回歸（中軸 TL），殘差母體標準差 SD
     * 畫 ±1SD、±2SD、-3SD、-4SD，延伸到最近 displayWindow 日。window / displayWindow 來自 config.json lohas。
     */
    function computeLohas(dates, closes, window, displayWindow) {
        const w = Math.trunc(window);
        const dw = Math.trunc(displayWindow);
        if (!(w > 0) || !(dw > 0)) throw new Error("computeLohas 需要 window 與 displayWindow（config.json lohas）");
        const len = Math.min(dates.length, closes.length);
        const start = Math.max(0, len - dw);
        const pairDates = dates.slice(start, len);
        const shown = closes.slice(start, len).map(Number);
        if (!pairDates.length) return {chartData: [], n: 0, window: w, insufficient: true};
        const reg = shown.slice(Math.max(0, shown.length - w));
        const n = reg.length;
        const xMean = (n - 1) / 2;
        const yMean = compensatedSum(reg) / n;
        const num = compensatedSum(reg.map((y, i) => (i - xMean) * (y - yMean)));
        const den = compensatedSum(reg.map((_y, i) => (i - xMean) * (i - xMean)));
        const slope = den ? num / den : 0;
        const intercept = yMean - slope * xMean;
        const residuals = reg.map((y, i) => y - (slope * i + intercept));
        const rMean = compensatedSum(residuals) / n;
        const sd = Math.sqrt(compensatedSum(residuals.map(r => (r - rMean) * (r - rMean))) / n);
        const offset = shown.length - n;
        const chartData = pairDates.map((date, i) => {
            const tl = slope * (i - offset) + intercept;
            return {
                date,
                close: round(shown[i], 2),
                tl: round(tl, 2),
                p1sd: round(tl + sd, 2),
                p2sd: round(tl + 2 * sd, 2),
                m1sd: round(tl - sd, 2),
                m2sd: round(tl - 2 * sd, 2),
                m3sd: round(tl - 3 * sd, 2),
                m4sd: round(tl - 4 * sd, 2),
            };
        });
        return {chartData, n, window: w, insufficient: n < w};
    }

    // ------------------------------------------------------------------ 持股彙總（對應 app.py）

    /** 對應 beta_bucket：cfg = APP_CONFIG.betaBuckets。 */
    function betaBucket(isCashPos, beta, cfg) {
        requireNumbers(cfg, ["cashBeta", "leveragedMin"], "betaBuckets");
        const known = beta !== null && beta !== undefined;
        if (isCashPos || (known && beta === cfg.cashBeta)) return "cash";
        if (!known) return "unclassified";
        if (beta >= cfg.leveragedMin) return "leveraged";
        return "index";
    }

    /**
     * 對應 summarize_positions：總額、占比、市場小計、Beta 分桶、加權 Beta。
     * 會就地寫入每筆 position.weight 並依市值排序；value 為 null 代表缺價（不計入總資產，列在 missingPrice）。
     */
    function summarizePositions(positions, fxRate, bucketCfg) {
        requireNumbers(bucketCfg, ["cashBeta", "leveragedMin"], "betaBuckets");
        let total = 0;
        const missingPrice = [];
        positions.forEach(p => {
            const value = nullish(p.value);
            if (value === null && (p.shares || 0) > 0 && !p.isCash) missingPrice.push(p.ticker);
            if (value) total += value;
        });

        let betaValueSum = 0;
        let betaWeightSum = 0;
        const betaUnknown = [];
        const breakdown = {us: 0, tw: 0, cash: 0};
        const buckets = {index: 0, leveraged: 0, cash: 0, unclassified: 0};
        positions.forEach(p => {
            const value = nullish(p.value);
            p.weight = value && total > 0 ? round(value / total * 100, 2) : 0;
            if (!value) return;
            if (p.isCash) breakdown.cash += value;
            else if (p.isTaiwan) breakdown.tw += value;
            else breakdown.us += value;
            buckets[betaBucket(p.isCash, nullish(p.beta), bucketCfg)] += value;
            const beta = p.isCash && nullish(p.beta) === null ? 0 : nullish(p.beta);
            if (beta === null) {
                betaUnknown.push(p.ticker);
            } else {
                betaValueSum += value * beta;
                betaWeightSum += value;
            }
        });

        positions.sort((a, b) => (b.value || 0) - (a.value || 0));
        const mapValues = (obj, fn) => {
            const out = {};
            Object.keys(obj).forEach(k => { out[k] = fn(obj[k]); });
            return out;
        };
        return {
            positions,
            totalValue: round(total, 2),
            totalValueUsd: fxRate ? round(total / fxRate, 2) : null,
            fxRate: nullish(fxRate),
            portfolioBeta: betaWeightSum > 0 ? round(betaValueSum / betaWeightSum, 4) : null,
            breakdown: mapValues(breakdown, v => round(v, 2)),
            breakdownUsd: fxRate ? mapValues(breakdown, v => round(v / fxRate, 2)) : null,
            betaAllocation: mapValues(buckets, v => (total > 0 ? round(v / total * 100, 2) : 0)),
            missingPrice,
            betaUnknown,
        };
    }

    // ------------------------------------------------------------------ 市場指標

    function round1(value) {
        return value !== null ? round(value, 1) : null;
    }

    /** 對應 parse_fear_greed：分數缺值或超出 0–100 時丟出錯誤。 */
    function parseFearGreed(payload) {
        const fg = ((payload || {}).fear_and_greed) || {};
        const score = toFloat(fg.score);
        if (score === null || !(score >= 0 && score <= 100)) throw new Error("score 缺值或超出 0–100");
        const rating = fg.rating ? String(fg.rating).toUpperCase() : "";
        return {
            score: round(score, 1),
            rating: rating || null,
            timestamp: nullish(fg.timestamp),
            previousClose: round1(toFloat(fg.previous_close)),
            previous1Week: round1(toFloat(fg.previous_1_week)),
            previous1Month: round1(toFloat(fg.previous_1_month)),
            previous1Year: round1(toFloat(fg.previous_1_year)),
            error: null,
        };
    }

    /** 對應 _pe_plausible：cfg = APP_CONFIG.forwardPe。 */
    function pePlausible(value, cfg) {
        requireNumbers(cfg, ["min", "max"], "forwardPe");
        return value !== null && value !== undefined && cfg.min <= value && value <= cfg.max;
    }

    const MONTHS = {Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12};

    /** 對應 _english_date：'Nov 21, 2026' → '2026-11-21'（格式不對回傳 null）。 */
    function englishDate(text) {
        const match = /^([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})$/.exec(String(text || "").trim());
        if (!match || !hasOwn(MONTHS, match[1])) return null;
        const y = Number(match[3]);
        const m = MONTHS[match[1]];
        const d = Number(match[2]);
        const check = new Date(Date.UTC(2000, m - 1, d));
        if (y < 1 || check.getUTCMonth() !== m - 1 || check.getUTCDate() !== d) return null;
        const leapOk = !(m === 2 && d === 29) || ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0);
        return leapOk ? pad(y, 4) + "-" + pad(m, 2) + "-" + pad(d, 2) : null;
    }

    /**
     * 對應 _parse_dollarliquidity_overview：總覽頁的 Nasdaq-100 Forward P/E。
     * change 是網站顯示的變化值（比較基準未說明），previousValue = value − change 只是推算（previousValueDerived）。
     * React 伺服器端輸出會在相鄰文字節點之間插入 <!-- -->（例如 "+<!-- -->1.68"）：先移除註解再把標籤換成空白，
     * 正負號與數字之間容許空白。
     */
    function parseDollarLiquidityOverview(htmlText) {
        const plain = String(htmlText || "").replace(/<!--[\s\S]*?-->/g, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
        const match = /Nasdaq-100 Forward P\/E\s+(\d+(?:\.\d+)?)\s*[×x].{0,400}?As of\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*([+−-]?)\s*(\d+(?:\.\d+)?)/
            .exec(plain);
        if (!match) return null;
        let val = toFloat(match[1]);
        let chg = toFloat(match[3].replace("−", "-") + match[4]);
        const date = englishDate(match[2]);
        if (val === null || chg === null) return null;
        val = round(val, 2);
        chg = round(chg, 2);
        return {
            value: val,
            date,
            previousValue: round(val - chg, 2),
            previousDate: null,
            previousValueDerived: true,
            change: chg,
        };
    }

    /** 對應 _parse_dollarliquidity_detail：明細頁 Today 數值 + historyPoints 倒數第二筆當前值。 */
    function parseDollarLiquidityDetail(text) {
        text = String(text || "");
        let prevVal = null;
        let prevDate = null;
        const hpIdx = text.indexOf("historyPoints");
        if (hpIdx > 0) {
            const snippet = text.slice(Math.max(0, hpIdx - 1200), hpIdx);
            const re = /\\?"date\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})\\?"\s*,\s*\\?"pe\\?"\s*:\s*([\d.]+)/g;
            const pairs = [];
            let m;
            while ((m = re.exec(snippet)) !== null) pairs.push([m[1], m[2]]);
            if (pairs.length >= 2) {
                prevDate = pairs[pairs.length - 2][0];
                prevVal = toFloat(pairs[pairs.length - 2][1]);
                if (prevVal === null) prevDate = null;
                else prevVal = round(prevVal, 2);
            }
        }
        let val = null;
        let date = null;
        const match = /Forward P\/E Ratio Today:\s*([\d.]+)\s*[×x]?\s*\((\d{4}-\d{2}-\d{2})\)/i.exec(text);
        if (match) {
            val = toFloat(match[1]);
            date = match[2];
        } else {
            const matchVal = /"name"\s*:\s*"Forward P\/E"\s*,\s*"unitText"\s*:\s*"ratio"\s*,\s*"value"\s*:\s*([\d.]+)/
                .exec(text);
            if (matchVal) {
                val = toFloat(matchVal[1]);
                const dateMatch = /"latestDate"\s*:\s*"(\d{4}-\d{2}-\d{2})"/.exec(text);
                date = dateMatch ? dateMatch[1] : null;
            }
        }
        if (val === null) return null;
        val = round(val, 2);
        return {
            value: val,
            date,
            previousValue: prevVal,
            previousDate: prevDate,
            previousValueDerived: false,
            change: prevVal !== null ? round(val - prevVal, 2) : null,
        };
    }

    // ------------------------------------------------------------------ 借貸攤銷

    function daysInMonth(y, m0) {
        return new Date(y, m0 + 1, 0).getDate();
    }

    /** 第 m0 個月（0 起算，可超過 11）的還款日；dueDay 超過當月天數時取月底（31 日 → 2 月 28/29 日）。 */
    function paymentDate(y, m0, dueDay) {
        const year = y + Math.floor(m0 / 12);
        const month = ((m0 % 12) + 12) % 12;
        return new Date(year, month, Math.min(dueDay, daysInMonth(year, month)));
    }

    function validDueDay(dueDay) {
        return Number.isInteger(dueDay) && dueDay >= 1 && dueDay <= 31;
    }

    function startOfDay(date) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate());
    }

    // fromDate 之後（含當天）第一個還款日的月份序號
    function firstPaymentMonth(fromDate, dueDay) {
        const y = fromDate.getFullYear();
        const m = fromDate.getMonth();
        return paymentDate(y, m, dueDay) < fromDate ? m + 1 : m;
    }

    /**
     * 從 fromDate（含當天：當天正好是還款日算尚未扣款、計入一期）到到期日共有幾個還款日。
     * 每月還款日固定為 dueDay（遇小月取月底），以月份計數前進，不會因為日期溢位跳過或重複月份。
     */
    function countRemainingPayments(dueDay, maturityDateStr, fromDate) {
        const maturity = parseLocalDate(maturityDateStr);
        if (!maturity || !validDueDay(dueDay)) return 0;
        const from = startOfDay(fromDate);
        const y = from.getFullYear();
        const m = firstPaymentMonth(from, dueDay);
        let count = 0;
        while (count < MAX_PAYMENTS && paymentDate(y, m + count, dueDay) <= maturity) count++;
        return count;
    }

    /**
     * 用年金公式（PV = PMT × (1-(1+r)^-n)/r）反推每期利率 r，二分搜尋法。
     * 回傳月利率；若 monthlyPayment × periods <= principal（不含利息或資料異常）回傳 0。
     */
    function solveMonthlyRate(principal, monthlyPayment, periods) {
        if (periods <= 0) return 0;
        if (monthlyPayment * periods <= principal) return 0;
        let lo = 0;
        let hi = 1;
        for (let i = 0; i < 100; i++) {
            const mid = (lo + hi) / 2;
            const pv = mid === 0
                ? monthlyPayment * periods
                : monthlyPayment * (1 - Math.pow(1 + mid, -periods)) / mid;
            if (pv > principal) lo = mid; else hi = mid;
        }
        return (lo + hi) / 2;
    }

    /**
     * 依基準日 asOfDate 反推的期數 / 利率攤銷到 today：每經過一個已發生（today 當天尚未算入）的還款日，
     * 本金扣掉當期本金部分；扣款次數不超過基準日時的剩餘期數。
     * 回傳 {principalToday, remainingToday, monthlyRate, elapsed}
     */
    function amortizeToToday(loan, today) {
        const todayStart = startOfDay(today);
        const asOf = parseLocalDate(loan.asOfDate) || todayStart;
        const n0 = countRemainingPayments(loan.dueDay, loan.maturityDate, asOf);
        const monthlyRate = solveMonthlyRate(loan.principal, loan.monthlyPayment, n0);
        let elapsed = 0;
        let principal = loan.principal;
        if (validDueDay(loan.dueDay)) {
            const y = asOf.getFullYear();
            const m = firstPaymentMonth(asOf, loan.dueDay);
            while (elapsed < n0 && paymentDate(y, m + elapsed, loan.dueDay) < todayStart) {
                const interest = principal * monthlyRate;
                principal = Math.max(0, principal - (loan.monthlyPayment - interest));
                elapsed++;
            }
        }
        const remainingToday = countRemainingPayments(loan.dueDay, loan.maturityDate, todayStart);
        return {principalToday: Math.round(principal), remainingToday, monthlyRate, elapsed};
    }

    return {
        // 文字 / 數值
        esc, toFloat, toPositive, toInt, isValidPrice, isValidTs, round, compensatedSum,
        // 代號
        normTicker, isTaiwanTicker, isCash, twseChannel, cnyesSymbol, twseChannelMap, cnyesSymbolMap,
        // 日期
        marketDate, parseLocalDate, localDateStr, taipeiDateStr, exchangeDateStr, daysOld, minutesSince,
        freshnessLevel, upsertLastPoint,
        // 報價
        chartCloses, dailyBars, prevCloseFromChart, yahooQuoteFromChart, vixFromChart, fxPlausible, fxInfoFromYahoo,
        firstPositiveFromBook, parseTwseRow, parseCnyesRow, matchTwseRows, matchCnyesRows,
        resolveConsensus, twQuote, usQuote, TW_PRIORITY, US_PRIORITY,
        // 五線譜 / 持股
        computeLohas, betaBucket, summarizePositions,
        // 市場指標
        parseFearGreed, pePlausible, englishDate, parseDollarLiquidityOverview, parseDollarLiquidityDetail,
        // 借貸
        paymentDate, countRemainingPayments, solveMonthlyRate, amortizeToToday,
    };
});
