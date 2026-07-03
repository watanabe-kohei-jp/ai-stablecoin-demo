// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * MockYieldVault — テストネット専用の「利回りVault」模擬（ERC-4626 風インターフェース）
 * =======================================================================
 * ⚠️ これは本物の RWA / トークン化MMF ではない。Base Sepolia 上の学習・デモ専用。
 *    本物（BlackRock BUIDL / Circle USYC 等）はメインネット＋許可制(KYC)で、
 *    本プロジェクトの鉄則「テストネット限定・本物資産に触れない」に反するため使わない。
 *
 * 目的：AIエージェントの「自動スイープ」を実演する。
 *    待機USDC → Vault に預けて利回り → 支払い直前に必要額だけ償還(JIT) → x402 決済。
 *
 * 利回りの出し方（本物の利回りモード）：
 *    あらかじめ Vault にテストUSDCを「準備金(reserve)」としてシードしておく。
 *    時間経過に応じて apy 分だけ accruedYield に算入し、redeem/withdraw 時に
 *    share価格が上がって元本+利回りが返る。準備金が無ければ利回り0＝元本保全。
 *
 * 会計の安全策（Codex レビュー反映）：
 *    - 全 mutating 関数の先頭で _accrue()（新規預入者が過去利回りを盗めない）
 *    - totalAssets() は principal+accruedYield のみ（生残高を返さない
 *      ＝ ERC-4626 の donation / first-depositor インフレ攻撃を構造的に封じる）
 *    - redeem/withdraw は元本と利回りを比例按分で減算
 *    - 最後の1人(shares==totalShares)は dust を残さず全清算
 *    - withdraw(assets) は必要share切り上げ（JITで1weiの不足を起こさない）
 *
 * 既知の割り切り（デモのため）：
 *    - 即時償還（償還レイテンシ＝流動性ミスマッチは記事の論点として別途文書化）
 *    - owner は apy 変更のみ（資金は引き出せない）
 */

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract MockYieldVault {
    IERC20  public immutable asset;        // 預け入れ資産（テストUSDC, decimals=6）
    address public owner;
    uint16  public apyBps;                 // 年率（bps）。例: 500 = 5%
    uint256 private constant YEAR = 365 days;

    uint256 public totalShares;
    mapping(address => uint256) public shares;

    uint256 public principal;              // 預けられた元本合計（コスト基準）
    uint256 public accruedYield;           // 算入済みの利回り
    uint256 public lastAccrue;             // 最後に算入した時刻

    event Deposit(address indexed who, uint256 assets, uint256 sharesMinted);
    event Withdraw(address indexed who, uint256 assets, uint256 sharesBurned);
    event Accrue(uint256 added, uint256 totalAssets);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor(address _asset, uint16 _apyBps) {
        require(_asset != address(0), "asset=0");
        asset = IERC20(_asset);
        owner = msg.sender;
        apyBps = _apyBps;
        lastAccrue = block.timestamp;
    }

    // ---- 利回り算入（全 mutating 関数の先頭で呼ぶ） ----
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

    // Vault保有USDC − (元本+確定利回り) = まだ利回りに回せる準備金
    function _reserveAvailable() internal view returns (uint256) {
        uint256 bal = asset.balanceOf(address(this));
        uint256 committed = principal + accruedYield;
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

    /// @notice 必要資産額 assets を引き出すのに burn すべき share（切り上げ＝JIT用）
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

    /// @notice 資産額を指定して引き出す（自動スイープのJITで使う）
    function withdraw(uint256 assets) external returns (uint256 burnedShares) {
        _accrue();
        require(assets > 0, "zero");
        burnedShares = previewWithdraw(assets);
        require(burnedShares > 0 && shares[msg.sender] >= burnedShares, "insufficient shares");
        _burnAndPay(msg.sender, burnedShares, assets);
        emit Withdraw(msg.sender, assets, burnedShares);
    }

    /// @notice share数を指定して償還する
    function redeem(uint256 sh) external returns (uint256 assetsOut) {
        _accrue();
        require(sh > 0 && shares[msg.sender] >= sh, "insufficient shares");
        assetsOut = (sh == totalShares) ? (principal + accruedYield) : convertToAssets(sh);
        _burnAndPay(msg.sender, sh, assetsOut);
        emit Withdraw(msg.sender, assetsOut, sh);
    }

    // 内部：share を burn し assets を払う。元本/利回りを比例按分で減算。
    function _burnAndPay(address who, uint256 sh, uint256 assetsOut) internal {
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
        require(asset.transfer(who, assetsOut), "transfer");
    }

    function setApy(uint16 _apyBps) external onlyOwner {
        _accrue();
        apyBps = _apyBps;
    }
}
