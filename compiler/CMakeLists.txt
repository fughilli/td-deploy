cmake_minimum_required(VERSION 3.20)
project(toxc LANGUAGES CXX C)

find_package(MLIR REQUIRED CONFIG)
message(STATUS "Using MLIRConfig.cmake in: ${MLIR_DIR}")
message(STATUS "Using LLVMConfig.cmake in: ${LLVM_DIR}")

list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(TableGen)
include(AddLLVM)
include(AddMLIR)
include(HandleLLVMOptions)

# nixpkgs MLIR sets *_TABLEGEN_EXE to bare names (no imported target), which ninja
# treats as missing files. Force the real executables resolved from PATH.
find_program(TOXC_MLIR_TBLGEN NAMES mlir-tblgen REQUIRED)
find_program(TOXC_LLVM_TBLGEN NAMES llvm-tblgen REQUIRED)
set(MLIR_TABLEGEN_EXE "${TOXC_MLIR_TBLGEN}" CACHE FILEPATH "" FORCE)
set(LLVM_TABLEGEN_EXE "${TOXC_LLVM_TBLGEN}" CACHE FILEPATH "" FORCE)
message(STATUS "mlir-tblgen: ${MLIR_TABLEGEN_EXE}")

include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)
include_directories(${CMAKE_CURRENT_BINARY_DIR}/include)
link_directories(${LLVM_BUILD_LIBRARY_DIR})
add_definitions(${LLVM_DEFINITIONS})

add_subdirectory(include/Tox)
add_subdirectory(lib)
add_subdirectory(tools/toxc-opt)
