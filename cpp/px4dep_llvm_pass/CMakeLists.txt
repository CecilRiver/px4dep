cmake_minimum_required(VERSION 3.16)
project(px4dep_llvm_pass LANGUAGES C CXX)

find_package(LLVM 14 REQUIRED CONFIG)

message(STATUS "Using LLVM ${LLVM_PACKAGE_VERSION} from ${LLVM_DIR}")

add_library(px4dep_member_topic MODULE member_topic_pass.cpp)

target_include_directories(px4dep_member_topic PRIVATE ${LLVM_INCLUDE_DIRS})
target_compile_options(px4dep_member_topic PRIVATE
  -fno-rtti
  -fno-exceptions
  -Wall
  -Wextra
)
separate_arguments(LLVM_DEFINITION_FLAGS UNIX_COMMAND "${LLVM_DEFINITIONS}")
target_compile_options(px4dep_member_topic PRIVATE ${LLVM_DEFINITION_FLAGS})
separate_arguments(LLVM_CXX_FLAGS UNIX_COMMAND "${LLVM_CXXFLAGS}")
target_compile_options(px4dep_member_topic PRIVATE ${LLVM_CXX_FLAGS})

set_target_properties(px4dep_member_topic PROPERTIES
  PREFIX ""
  OUTPUT_NAME "px4dep_member_topic"
)
