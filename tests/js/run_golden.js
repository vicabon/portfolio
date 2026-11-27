/**
 * 黃金測試執行器：core.js 對 tests/golden/*.json 的每個輸入，必須算出與 Python 相同的結果。
 *
 *   node tests/js/run_golden.js                      # 用本機時區
 *   TZ=America/New_York node tests/js/run_golden.js  # tests/test_js_golden.py 會用三個時區各跑一次
 *
 * 也可以在瀏覽器執行：先載入 core.js 與本檔，再呼叫 GoldenRunner.runAll(window.PortfolioCore, goldens)。
 * 比較規則與 tests/make_golden.py 的 _diff 相同，但更嚴格一點：
 * 期望 null 時 undefined / NaN 都算錯，期望數字時 NaN / Infinity 也算錯（不靠 JSON 轉換蓋掉差異）。
 */
(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    } else {
        root.GoldenRunner = api;
    }
})(typeof self !== "undefined" ? self : this, function () {
    "use strict";

    function cloneJson(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function describe(value) {
        if (value === undefined) return "undefined";
        if (typeof value === "number" && !Number.isFinite(value)) return String(value);
        if (value instanceof Date) return "Date(" + value.toString() + ")";
        let text;
        try {
            text = JSON.stringify(value);
        } catch (error) {
            text = String(value);
        }
        return text.length > 80 ? text.slice(0, 77) + "..." : text;
    }

    function isPlainObject(value) {
        return value !== null && typeof value === "object" && !Array.isArray(value) && !(value instanceof Date);
    }

    /** 回傳第一個差異的說明；完全一致回傳 null。tol 為相對誤差（只用在數值）。 */
    function diff(actual, expected, tol, path) {
        const mismatch = () => path + ": " + describe(actual) + " != " + describe(expected);
        if (expected === null) return actual === null ? null : mismatch();
        if (typeof expected === "boolean" || typeof expected === "string") {
            return actual === expected ? null : mismatch();
        }
        if (typeof expected === "number") {
            if (typeof actual !== "number" || !Number.isFinite(actual)) return mismatch();
            if (actual === expected) return null;
            if (tol && Math.abs(actual - expected) <= tol * Math.max(Math.abs(actual), Math.abs(expected))) {
                return null;
            }
            return mismatch();
        }
        if (Array.isArray(expected)) {
            if (!Array.isArray(actual)) return mismatch();
            if (actual.length !== expected.length) {
                return path + ": 長度 " + actual.length + " != " + expected.length;
            }
            for (let i = 0; i < expected.length; i++) {
                const problem = diff(actual[i], expected[i], tol, path + "[" + i + "]");
                if (problem) return problem;
            }
            return null;
        }
        if (isPlainObject(expected)) {
            if (!isPlainObject(actual)) return mismatch();
            // JSON 沒有 undefined：值為 undefined 的欄位視為不存在（與 Python 端的 JSON 輸出一致）
            const actualKeys = Object.keys(actual).filter(key => actual[key] !== undefined);
            const keys = Array.from(new Set(actualKeys.concat(Object.keys(expected)))).sort();
            for (const key of keys) {
                if (!Object.prototype.hasOwnProperty.call(expected, key)) return path + "." + key + ": 多出的欄位";
                if (actualKeys.indexOf(key) < 0) return path + "." + key + ": 缺少欄位";
                const problem = diff(actual[key], expected[key], tol, path + "." + key);
                if (problem) return problem;
            }
            return null;
        }
        return path + ": golden 含無法比較的期望值 " + describe(expected);
    }

    function convertArgs(C, testCase) {
        const args = cloneJson(testCase.args);
        const types = testCase.argTypes || [];
        return args.map((arg, i) => {
            if (types[i] !== "date") return arg;
            const date = C.parseLocalDate(arg);
            if (date === null) throw new Error("argTypes 指定 date，但 " + describe(arg) + " 不是合法日期");
            return date;
        });
    }

    function convertResult(C, testCase, result) {
        if (testCase.resultType === "date" && result instanceof Date) return C.localDateStr(result);
        return result;
    }

    /** 執行單一 case：通過回傳 null，否則回傳失敗說明。 */
    function runCase(C, testCase) {
        const fn = C[testCase.js];
        if (typeof fn !== "function") return "core.js 沒有匯出 " + testCase.js;
        let result;
        try {
            result = convertResult(C, testCase, fn.apply(null, convertArgs(C, testCase)));
        } catch (error) {
            if (testCase.throws) return null;
            return "丟出未預期的錯誤：" + (error && error.message ? error.message : String(error));
        }
        if (testCase.throws) return "預期丟出錯誤，實際回傳 " + describe(result);
        return diff(result, testCase.expected, testCase.tol, "結果");
    }

    /** goldens：{檔名: golden 內容}。回傳 {passed, failed, failures: [{file, name, problem}]}。 */
    function runAll(C, goldens) {
        const report = {passed: 0, failed: 0, failures: []};
        for (const file of Object.keys(goldens).sort()) {
            const cases = (goldens[file] && goldens[file].cases) || [];
            if (!cases.length) {
                report.failed += 1;
                report.failures.push({file, name: "(整個檔案)", problem: "沒有任何 case"});
                continue;
            }
            for (const testCase of cases) {
                const problem = runCase(C, testCase);
                if (problem) {
                    report.failed += 1;
                    report.failures.push({file, name: testCase.js + " / " + testCase.name, problem});
                } else {
                    report.passed += 1;
                }
            }
        }
        return report;
    }

    return {runAll, runCase, diff};
});

if (typeof require === "function" && typeof module === "object" && require.main === module) {
    const fs = require("fs");
    const path = require("path");
    const runner = module.exports;
    const core = require(path.join(__dirname, "..", "..", "core.js"));
    const goldenDir = path.join(__dirname, "..", "golden");
    const goldens = {};
    for (const file of fs.readdirSync(goldenDir).filter(name => name.endsWith(".json")).sort()) {
        goldens[file] = JSON.parse(fs.readFileSync(path.join(goldenDir, file), "utf8"));
    }
    const tz = process.env.TZ || Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!Object.keys(goldens).length) {
        console.log("FAIL 找不到 tests/golden/*.json");
        process.exitCode = 1;
    } else {
        const report = runner.runAll(core, goldens);
        for (const failure of report.failures) {
            console.log("FAIL " + failure.file + " :: " + failure.name + " :: " + failure.problem);
        }
        console.log("golden（TZ=" + tz + "）：" + report.passed + " 通過、" + report.failed + " 失敗");
        process.exitCode = report.failed ? 1 : 0;
    }
}
