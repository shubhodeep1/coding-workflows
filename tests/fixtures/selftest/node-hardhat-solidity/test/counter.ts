import { expect } from "chai";
import { ethers } from "hardhat";

describe("Counter", function () {
	it("increments", async function () {
		const Counter = await ethers.getContractFactory("Counter");
		const counter = await Counter.deploy();
		await counter.waitForDeployment();
		await counter.increment();
		expect(await counter.value()).to.equal(1n);
	});
});
