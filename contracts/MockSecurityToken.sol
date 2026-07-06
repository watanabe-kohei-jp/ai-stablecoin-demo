// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * MockSecurityToken — テストネット専用の「セキュリティトークン（ST）」模擬
 * =======================================================================
 * ⚠️ これは本物のセキュリティトークン / RWA ではない。Base Sepolia 上の学習・デモ専用。
 *
 * 目的：「流動性の階段」の一番奥の段を実演する。
 *    ・利回りは高め（準備金から払う。会計は MockYieldVault と同型）
 *    ・ただし【即時償還できない】＝ requestRedeem → redeemDelay 経過 → claimRedeem
 *      の二段階償還。これが本物の RWA が持つ「償還レイテンシ」
 *      （24/7 の約束 vs 原資産は T+1、という流動性ミスマッチ）の模擬。
 *
 * MockYieldVault との差分：
 *    - withdraw / redeem（即時）を持たない
 *    - requestWithdraw(assets) / requestRedeem(shares) で償還を「予約」し、
 *      claimRedeem(requestId) で redeemDelay 経過後に受け取る
 *    - 予約時点で share を burn し会計から切り離す（NAV は予約時点で確定）。
 *      支払い待ちの資産は totalPendingAssets として区分し、
 *      準備金計算（_reserveAvailable）から除外する＝二重計上を防ぐ
 *
 * 会計の安全策は MockYieldVault と同じ：
 *    - 全 mutating 関数の先頭で _accrue()
 *    - totalAssets() は principal+accruedYield のみ（donation 攻撃封じ）
 *    - 償還は元本と利回りを比例按分で減算、最後の1人は dust なし全清算
 */

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract MockSecurityToken {
    IERC20  public immutable asset;        // 預け入れ資産（テストUSDC, decimals=6）
    address public owner;
    uint16  public apyBps;                 // 年率（bps）。ST想定なので高め（例: 800 = 8%）
    uint256 public immutable redeemDelay;  // 償還ラグ（秒）。テストネットでは短く模擬（例: 120）
    uint256 private constant YEAR = 365 days;

    uint256 public totalShares;
    mapping(address => uint256) public shares;

    uint256 public principal;              // 預けられた元本合計（コスト基準）
    uint256 public accruedYield;           // 算入済みの利回り
    uint256 public lastAccrue;             // 最後に算入した時刻

    // ---- 二段階償還 ----
    struct RedemptionRequest {
        address who;        // 予約者
        uint256 assets;     // 予約時に確定した支払額（NAV確定）
        uint64  unlockAt;   // この時刻以降に claim 可能
        bool    claimed;
    }
    RedemptionRequest[] public requests;
    uint256 public totalPendingAssets;     // 支払い待ちの合計（準備金計算から除外する）

    event Deposit(address indexed who, uint256 assets, uint256 sharesMinted);
    event RedeemRequested(address indexed who, uint256 indexed requestId, uint256 assets, uint64 unlockAt);
    event RedeemClaimed(address indexed who, uint256 indexed requestId, uint256 assets);
    event Accrue(uint256 added, uint256 totalAssets);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(address _asset, uint16 _apyBps, uint256 _redeemDelay) {
        require(_asset != address(0), "asset=0");
        asset = IERC20(_asset);
        owner = msg.sender;
        apyBps = _apyBps;
        redeemDelay = _redeemDelay;
        lastAccrue = block.timestamp;
    }

    // ---- 利回り算入（MockYieldVault と同型） ----
    function _accrue() internal {
        uint256 committed = principal + accruedYield;
        uint256 dt = block.timestamp - lastAccrue;
        if (committed > 0 && apyBps > 0 && dt > 0) {
            uint256 add = (committed * apyBps * dt) / (10000 * YEAR);
            uint256 reserve = _reserveAvailable();
            if (add > reserve) add = reserve;   // 準備金を超えては払えない
            if (add > 0) {
                accruedYield += add;
                emit Accrue(add, principal + accruedYield);
            }
        }
        lastAccrue = block.timestamp;
    }

    /// @notice 経過分の利回りを誰でも算入できる（デモ表示用）
    function poke() external {
        _accrue();
    }

    // Vault保有USDC − (元本+確定利回り) − 支払い待ち = まだ利回りに回せる準備金
    function _reserveAvailable() internal view returns (uint256) {
        uint256 bal = asset.balanceOf(address(this));
        uint256 committed = principal + accruedYield + totalPendingAssets;
        return bal > committed ? bal - committed : 0;
    }

    /// @notice share価格の基準。生残高ではなく principal+accruedYield のみ（攻撃封じ）
    function totalAssets() public view returns (uint256) {
        return principal + accruedYield;
    }

    function convertToShares(uint256 assets) public view returns (uint256) {
        uint256 ta = totalAssets();
        if (totalShares == 0 || ta == 0) return assets;     // 初期は 1:1
        return (assets * totalShares) / ta;                 // floor（控えめにmint）
    }

    function convertToAssets(uint256 sh) public view returns (uint256) {
        if (totalShares == 0) return sh;
        return (sh * totalAssets()) / totalShares;          // floor
    }

    /// @notice 必要資産額 assets の償還予約に burn すべき share（切り上げ）
    function previewWithdraw(uint256 assets) public view returns (uint256) {
        uint256 ta = totalAssets();
        if (totalShares == 0 || ta == 0) return assets;
        return (assets * totalShares + ta - 1) / ta;        // ceil
    }

    function deposit(uint256 assets) external returns (uint256 mintedShares) {
        _accrue();
        require(assets > 0, "zero");
        mintedShares = convertToShares(assets);
        require(mintedShares > 0, "shares=0");
        require(asset.transferFrom(msg.sender, address(this), assets), "transferFrom");
        totalShares += mintedShares;
        shares[msg.sender] += mintedShares;
        principal += assets;
        emit Deposit(msg.sender, assets, mintedShares);
    }

    /// @notice 資産額を指定して償還を予約する（即時には受け取れない）
    function requestWithdraw(uint256 assets) external returns (uint256 requestId) {
        _accrue();
        require(assets > 0, "zero");
        uint256 sh = previewWithdraw(assets);
        require(sh > 0 && shares[msg.sender] >= sh, "insufficient shares");
        // 切り上げの結果 sh == totalShares（実質全額償還）になった場合、
        // earmark も全額(principal+accruedYield)に丸める。指定額のままにすると
        // 全 share burn なのに差額NAVが準備金へ漏れ、将来利回りに化けてしまう（Codex#1）
        uint256 assetsOut = (sh == totalShares) ? (principal + accruedYield) : assets;
        _burnAndEarmark(msg.sender, sh, assetsOut);
        requestId = _pushRequest(msg.sender, assetsOut);
    }

    /// @notice share数を指定して償還を予約する
    function requestRedeem(uint256 sh) external returns (uint256 requestId) {
        _accrue();
        require(sh > 0 && shares[msg.sender] >= sh, "insufficient shares");
        uint256 assetsOut = (sh == totalShares) ? (principal + accruedYield) : convertToAssets(sh);
        _burnAndEarmark(msg.sender, sh, assetsOut);
        requestId = _pushRequest(msg.sender, assetsOut);
    }

    /// @notice redeemDelay 経過後に、予約した資産を受け取る
    function claimRedeem(uint256 requestId) external returns (uint256 assetsOut) {
        require(requestId < requests.length, "bad id");
        RedemptionRequest storage r = requests[requestId];
        require(r.who == msg.sender, "not requester");
        require(!r.claimed, "claimed");
        require(block.timestamp >= r.unlockAt, "still locked");  // ← 流動性ミスマッチの核
        r.claimed = true;
        totalPendingAssets -= r.assets;
        assetsOut = r.assets;
        require(asset.transfer(msg.sender, assetsOut), "transfer");
        emit RedeemClaimed(msg.sender, requestId, assetsOut);
    }

    /// @notice 予約の照会用（wallet側のポーリングで使う）
    function requestCount() external view returns (uint256) {
        return requests.length;
    }

    // 内部：share を burn し会計から切り離す（支払いはせず earmark のみ）
    function _burnAndEarmark(address who, uint256 sh, uint256 assetsOut) internal {
        if (sh == totalShares) {
            // 最後の1人：dust を残さず全清算
            principal = 0;
            accruedYield = 0;
        } else {
            uint256 fromYield = (accruedYield * sh) / totalShares;
            if (fromYield > accruedYield) fromYield = accruedYield;
            uint256 fromPrincipal = assetsOut > fromYield ? (assetsOut - fromYield) : 0;
            if (fromPrincipal > principal) fromPrincipal = principal;
            accruedYield -= fromYield;
            principal -= fromPrincipal;
        }
        shares[who] -= sh;
        totalShares -= sh;
    }

    function _pushRequest(address who, uint256 assetsOut) internal returns (uint256 id) {
        totalPendingAssets += assetsOut;
        uint64 unlockAt = uint64(block.timestamp + redeemDelay);
        id = requests.length;
        requests.push(RedemptionRequest({who: who, assets: assetsOut, unlockAt: unlockAt, claimed: false}));
        emit RedeemRequested(who, id, assetsOut, unlockAt);
    }

    function setApy(uint16 _apyBps) external onlyOwner {
        _accrue();
        apyBps = _apyBps;
    }
}
