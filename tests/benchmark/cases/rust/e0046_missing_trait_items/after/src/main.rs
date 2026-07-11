trait Shape {
    fn area(&self) -> f64;
}
struct Circle { r: f64 }
impl Shape for Circle {
    fn area(&self) -> f64 { 3.14 * self.r * self.r }
}
fn main() {}
