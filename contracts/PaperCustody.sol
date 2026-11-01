// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title PaperCustody
/// @notice Records encrypted exam-paper metadata, printer authorization, key release,
///         and custody/printing reports. The contract never stores the raw AES key.
/// @dev The wrapped key is public after release; only the registered printer should
///      be able to unwrap it using its corresponding private key.
contract PaperCustody {
    address public examBoard;

    modifier onlyExamBoard() {
        require(msg.sender == examBoard, "Only exam board can call this");
        _;
    }

    constructor() {
        examBoard = msg.sender;
    }

    struct CenterPaper {
        bytes32 blobHash;
        string variantId;
        uint256 examStartTime;
        address printerAddress;
        uint256 expectedCopies;
        uint256 printedCount;
        bytes wrappedKey;
        bool keyReleased;
        bool registered;
    }

    struct CustodyEvent {
        string stage;
        address actor;
        uint256 timestamp;
        bytes32 hashAtEvent;
    }

    mapping(string => CenterPaper) private centerPapers;
    mapping(string => CustodyEvent[]) private custodyLog;

    event PaperRegistered(
        string centerId,
        bytes32 blobHash,
        string variantId,
        uint256 examStartTime,
        address printerAddress,
        uint256 expectedCopies
    );
    event CustodyLogged(string centerId, string stage, address actor, uint256 timestamp, bytes32 hashAtEvent);
    event KeyReleased(string centerId, uint256 timestamp);
    event PrintCountReported(string centerId, address printerAddress, uint256 printedCount, uint256 timestamp);

    /// @notice Register paper metadata. No key material is accepted at registration.
    /// @param blobHash Hash of the encrypted PDF blob, calculated off-chain.
    /// @param examStartTime Unix timestamp at/after which the wrapped key may be released.
    function registerPaper(
        string calldata centerId,
        bytes32 blobHash,
        string calldata variantId,
        uint256 examStartTime,
        address printerAddress,
        uint256 expectedCopies
    ) external onlyExamBoard {
        require(bytes(centerId).length > 0, "Empty center ID");
        require(!centerPapers[centerId].registered, "Center already registered");
        require(blobHash != bytes32(0), "Empty blob hash");
        require(bytes(variantId).length > 0, "Empty variant ID");
        require(examStartTime > block.timestamp, "Exam start must be in the future");
        require(printerAddress != address(0), "Invalid printer address");
        require(expectedCopies > 0, "Expected copies must be positive");

        centerPapers[centerId] = CenterPaper({
            blobHash: blobHash,
            variantId: variantId,
            examStartTime: examStartTime,
            printerAddress: printerAddress,
            expectedCopies: expectedCopies,
            printedCount: 0,
            wrappedKey: new bytes(0),
            keyReleased: false,
            registered: true
        });

        emit PaperRegistered(
            centerId, blobHash, variantId, examStartTime, printerAddress, expectedCopies
        );
    }

    /// @notice Release a printer-wrapped AES key at/after examStartTime.
    /// @dev The contract cannot verify the cryptographic wrapping; that is performed off-chain.
    function releaseKey(string calldata centerId, bytes calldata wrappedKey) external onlyExamBoard {
        CenterPaper storage paper = centerPapers[centerId];
        require(paper.registered, "Unknown center");
        require(block.timestamp >= paper.examStartTime, "Key not yet releasable");
        require(!paper.keyReleased, "Key already released");
        require(wrappedKey.length > 0 && wrappedKey.length <= 512, "Invalid wrapped key length");

        paper.wrappedKey = wrappedKey;
        paper.keyReleased = true;
        emit KeyReleased(centerId, block.timestamp);
    }

    /// @notice Record a custody event from the exam board or the center's registered printer.
    function logCustodyEvent(
        string calldata centerId,
        string calldata stage,
        bytes32 hashAtEvent
    ) external {
        CenterPaper storage paper = centerPapers[centerId];
        require(paper.registered, "Unknown center");
        require(msg.sender == examBoard || msg.sender == paper.printerAddress, "Not an authorized participant");
        require(bytes(stage).length > 0, "Empty stage");

        custodyLog[centerId].push(CustodyEvent({
            stage: stage,
            actor: msg.sender,
            timestamp: block.timestamp,
            hashAtEvent: hashAtEvent
        }));

        emit CustodyLogged(centerId, stage, msg.sender, block.timestamp, hashAtEvent);
    }

    /// @notice Record a printer-reported cumulative number of printed copies.
    /// @dev This records a claim from the printer; it cannot independently prove physical output.
    function reportPrintedCount(string calldata centerId, uint256 newCount) external {
        CenterPaper storage paper = centerPapers[centerId];
        require(paper.registered, "Unknown center");
        require(msg.sender == paper.printerAddress, "Only registered printer can report");
        require(paper.keyReleased, "Key not released");
        require(newCount >= paper.printedCount, "Printed count cannot decrease");
        require(newCount <= paper.expectedCopies, "Printed count exceeds expected copies");

        paper.printedCount = newCount;
        emit PrintCountReported(centerId, msg.sender, newCount, block.timestamp);
    }

    /// @notice Return the wrapped AES key after release. This value is public on-chain.
    function getKey(string calldata centerId) external view returns (bytes memory) {
        CenterPaper storage paper = centerPapers[centerId];
        require(paper.registered, "Unknown center");
        require(paper.keyReleased, "Key not released yet");
        return paper.wrappedKey;
    }

    function getBlobHash(string calldata centerId) external view returns (bytes32) {
        require(centerPapers[centerId].registered, "Unknown center");
        return centerPapers[centerId].blobHash;
    }

    function getVariantId(string calldata centerId) external view returns (string memory) {
        require(centerPapers[centerId].registered, "Unknown center");
        return centerPapers[centerId].variantId;
    }

    function isKeyReleased(string calldata centerId) external view returns (bool) {
        require(centerPapers[centerId].registered, "Unknown center");
        return centerPapers[centerId].keyReleased;
    }

    /// @notice Stable state tuple expected by the updated Python web3 client.
    function getCenterState(string calldata centerId)
        external
        view
        returns (
            bool registered,
            bool keyReleased,
            address printer,
            uint256 expectedCopies,
            uint256 printedCount,
            uint256 examStartTime
        )
    {
        CenterPaper storage paper = centerPapers[centerId];
        return (
            paper.registered,
            paper.keyReleased,
            paper.printerAddress,
            paper.expectedCopies,
            paper.printedCount,
            paper.examStartTime
        );
    }

    function getCustodyLog(string calldata centerId) external view returns (CustodyEvent[] memory) {
        require(centerPapers[centerId].registered, "Unknown center");
        return custodyLog[centerId];
    }
}
