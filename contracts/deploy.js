// deploy.js (Hardhat 3)
import fs from "fs";
import hre from "hardhat";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function main() {
  // Hardhat 3: ethers is no longer on `hre` directly -- you open a
  // connection to the selected network first, and it hands back ethers
  // scoped to that connection.
  const { ethers } = await hre.network.connect();

  const [deployer] = await ethers.getSigners();

  console.log("Deploying PaperCustody with account:", deployer.address);

  const balance = await ethers.provider.getBalance(deployer.address);
  console.log("Account balance:", ethers.formatEther(balance), "ETH");

  const PaperCustody = await ethers.getContractFactory("PaperCustody");
  const paperCustody = await PaperCustody.deploy();

  await paperCustody.waitForDeployment();

  const contractAddress = await paperCustody.getAddress();
  console.log("PaperCustody deployed to:", contractAddress);
  console.log("Exam board (deployer) set to:", deployer.address);

  const artifact = await hre.artifacts.readArtifact("PaperCustody");

  const deploymentInfo = {
    address: contractAddress,
    abi: artifact.abi,
    deployer: deployer.address,
    network: hre.network.name,
    deployedAt: new Date().toISOString(),
  };

  const outputDir = path.join(__dirname, "..", "backend");
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  const outputPath = path.join(outputDir, "contract_deployment.json");
  fs.writeFileSync(outputPath, JSON.stringify(deploymentInfo, null, 2));

  console.log("Deployment info written to:", outputPath);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });