cmake_minimum_required(VERSION 3.24)
project(deadzone_backend_build LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(UNREALSDK_FLAVOUR WILLOW CACHE STRING "UnrealSDK game flavour")
add_subdirectory(pyunrealsdk EXCLUDE_FROM_ALL)
pyunrealsdk_add_module(deadzone_backend
    deadzone_backend.cpp)
