// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Trade logger for demo / analytics.
 * - TradeOpen increments an internal counter and emits the new tradeId.
 * - TradeClosed now includes `sellAmount` to support partial closes downstream.
 */
contract TradeLogger {
    event TradeOpen(
        uint256 indexed tradeId,
        address indexed trader,
        address indexed tokenAddress,
        string tokenSymbol,
        string strategy,
        string action,
        uint256 entryPrice,   // scaled by PRICE_SCALE
        uint256 amount,       // base units (token decimals)
        uint256 timestamp
    );

    event TradeClosed(
        uint256 indexed tradeId,
        address indexed trader,
        address indexed tokenAddress,
        string tokenSymbol,
        uint256 exitPrice,    // scaled by PRICE_SCALE
        int256  pnl,          // scaled by PRICE_SCALE
        uint256 sellAmount,   // base units (token decimals)  <-- NEW
        uint256 timestamp
    );

    uint256 public tradeCounter;

    function logTradeOpen(
        address tokenAddress,
        string calldata tokenSymbol,
        string calldata strategy,
        string calldata action,
        uint256 entryPrice,
        uint256 amount
    ) external {
        uint256 id = ++tradeCounter;
        emit TradeOpen(
            id,
            msg.sender,
            tokenAddress,
            tokenSymbol,
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
        string calldata tokenSymbol,
        uint256 exitPrice,
        int256 pnl,
        uint256 sellAmount
    ) external {
        emit TradeClosed(
            tradeId,
            msg.sender,
            tokenAddress,
            tokenSymbol,
            exitPrice,
            pnl,
            sellAmount,
            block.timestamp
        );
    }
}
