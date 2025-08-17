// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

contract TradeLogger {
    event TradeOpen(
        uint256 indexed tradeId,
        address indexed trader,
        address indexed tokenAddress,
        string strategy,
        string action,          // "BUY" or "SELL"
        uint256 entryPrice,     // integer (scaled or raw)
        uint256 amount,         // token amount in smallest units (raw)
        uint256 timestamp
    );

    event TradeClosed(
        uint256 indexed tradeId,
        address indexed trader,
        address indexed tokenAddress,
        uint256 exitPrice,      // integer (scaled or raw)
        int256 pnl,             // signed PnL in quote units (scaled or raw)
        uint256 timestamp
    );

    uint256 public tradeCounter;

    function logTradeOpen(
        address tokenAddress,
        string memory strategy,
        string memory action,
        uint256 entryPrice,
        uint256 amount
    ) external {
        tradeCounter++;
        emit TradeOpen(
            tradeCounter,
            msg.sender,
            tokenAddress,
            strategy,
            action,
            entryPrice,
            amount,
            block.timestamp
        );
    }

    function logTradeClosed(
        uint256 tradeId,
        address tokenAddress,
        uint256 exitPrice,
        int256 pnl
    ) external {
        emit TradeClosed(
            tradeId,
            msg.sender,
            tokenAddress,
            exitPrice,
            pnl,
            block.timestamp
        );
    }
}