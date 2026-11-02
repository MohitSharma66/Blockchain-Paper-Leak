import hardhatToolboxMochaEthersPlugin from "@nomicfoundation/hardhat-toolbox-mocha-ethers";
import { configVariable, defineConfig } from "hardhat/config";

export default defineConfig({
  plugins: [hardhatToolboxMochaEthersPlugin],

  solidity: {
    version: "0.8.34",
    // The private Besu chain runs the Berlin/London rules. solc 0.8.20+ emits PUSH0 (Shanghai) and
    // newer ones MCOPY (Cancun) by default, which such a chain rejects ("invalid opcode").
    // Compile for Paris so the same bytecode runs on Besu, Hardhat and Sepolia.
    settings: { evmVersion: "paris" },
  },

  networks: {
    // Private permissioned chain (Besu QBFT in Docker). Deploy with:
    //   npx hardhat run scripts/deploy.js --network private
    // The deployer account becomes the contract's examBoard, so BOARD_PRIVATE_KEY must be the same
    // key the backend uses.  Gas is free on this chain.
    private: {
      type: "http",
      chainType: "l1",
      url: process.env.CHAIN_RPC_URL ?? "http://127.0.0.1:8545",
      chainId: 7357,
      gasPrice: 0,
      accounts: [configVariable("BOARD_PRIVATE_KEY")],
    },

    // Kept for reference; no longer used (the project moved off public testnets).
    sepolia: {
      type: "http",
      chainType: "l1",
      url: configVariable("SEPOLIA_RPC_URL"),
      accounts: [configVariable("SEPOLIA_PRIVATE_KEY")],
    },
  },
});
