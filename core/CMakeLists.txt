cmake_minimum_required(VERSION 3.20)
project(mc_pricer LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

option(MC_BUILD_PYTHON "Build the pybind11 module" ON)
option(MC_BUILD_TESTS "Build the GoogleTest suite" OFF)
set(MC_SANITIZE "" CACHE STRING "Sanitizers, e.g. address,undefined or thread")

if(MC_SANITIZE)
  add_compile_options(-fsanitize=${MC_SANITIZE} -fno-omit-frame-pointer -fno-sanitize-recover=all)
  add_link_options(-fsanitize=${MC_SANITIZE})
endif()

find_package(Threads REQUIRED)
add_library(mc_core STATIC src/analytic.cpp src/exposure.cpp src/parallel.cpp src/pricing.cpp)
target_include_directories(mc_core PUBLIC include)
target_compile_options(mc_core PRIVATE -Wall -Wextra -Wpedantic -Wshadow)
target_link_libraries(mc_core PUBLIC Threads::Threads)

if(MC_BUILD_PYTHON)
  find_package(Python 3.10 REQUIRED COMPONENTS Interpreter Development.Module)
  find_package(pybind11 CONFIG REQUIRED)
  pybind11_add_module(pricer_core python/bindings.cpp)
  target_link_libraries(pricer_core PRIVATE mc_core)
  install(TARGETS pricer_core LIBRARY DESTINATION .)
endif()

if(MC_BUILD_TESTS)
  enable_testing()
  find_package(GTest QUIET)
  if(NOT GTest_FOUND)
    include(FetchContent)
    FetchContent_Declare(googletest
      URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz
      DOWNLOAD_EXTRACT_TIMESTAMP ON)
    set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(googletest)
  endif()
  add_executable(mc_tests
    tests/test_random.cpp tests/test_analytic.cpp tests/test_pricing.cpp
    tests/test_exposure.cpp tests/test_parallel.cpp tests/test_memory.cpp)
  target_link_libraries(mc_tests PRIVATE mc_core GTest::gtest_main)
  include(GoogleTest)
  gtest_discover_tests(mc_tests DISCOVERY_MODE PRE_TEST)
endif()
