import re


def norm_tokens(text):
    out = []
    for piece in re.split(r"[\s\-—]+", text.lower()):
        piece = re.sub(r"[^a-z']", "", piece)
        if piece:
            out.append(piece)
    return out


def align(ref, hyp):
    """Levenshtein with backtrace. Returns (S, D, I, matches[(ref_i, hyp_j)])."""
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    back = [bytearray(m + 1) for _ in range(n + 1)]  # 0 diag-match,1 diag-sub,2 del(ref only),3 ins(hyp only)
    for j in range(1, m + 1):
        back[0][j] = 3
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        back[i][0] = 2
        ri = ref[i - 1]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                best, b = prev[j - 1], 0
            else:
                best, b = prev[j - 1] + 1, 1
            d = prev[j] + 1
            if d < best:
                best, b = d, 2
            ins = cur[j - 1] + 1
            if ins < best:
                best, b = ins, 3
            cur[j] = best
            back[i][j] = b
        prev = cur
    i, j = n, m
    S = D = I = 0
    matches = []
    while i > 0 or j > 0:
        b = back[i][j]
        if b == 0:
            matches.append((i - 1, j - 1)); i -= 1; j -= 1
        elif b == 1:
            S += 1; i -= 1; j -= 1
        elif b == 2:
            D += 1; i -= 1
        else:
            I += 1; j -= 1
    return S, D, I, matches[::-1]


def score(truth, words_hyp, start=0.0, end=1e12):
    """truth = corpus truth json dict; words_hyp = [(text, start_seconds_on_essence)].
    Returns metrics for the [start, end) region."""
    ref, ref_t = [], []
    for w in truth["words"]:
        if start <= w["t"] < end:
            for tok in norm_tokens(w["raw"]):
                ref.append(tok); ref_t.append(w["t"])
    gaps = [(g[0], g[1]) for g in truth["silences"] if g[1] > start and g[0] < end]
    hyp, hyp_t = [], []
    for text, t in words_hyp:
        if start <= t < end:
            for tok in norm_tokens(text):
                hyp.append(tok); hyp_t.append(t)
    S, D, I, matches = align(ref, hyp)
    errs = [hyp_t[j] - ref_t[i] for i, j in matches]
    ab = sorted(abs(e) for e in errs)
    inv = [h for h, t in zip(hyp, hyp_t) if any(g0 + 0.5 <= t <= g1 - 0.5 for g0, g1 in gaps)]
    return {"ref_words": len(ref), "hyp_words": len(hyp), "wer": round((S + D + I) / len(ref), 4), "S": S, "D": D, "I": I,
            "start_mae_s": round(sum(ab) / len(ab), 3), "start_p95_abs": round(ab[int(len(ab) * .95)], 3),
            "start_bias_s": round(sum(errs) / len(errs), 3), "start_max_abs": round(ab[-1], 3), "gap_words": len(inv)}
