pub fn total(value: i32) -> i32 { example_math::double(value) }

#[cfg(test)]
mod tests {
    #[test]
    fn doubles_input() { assert_eq!(super::total(2), 4); }
}
